"""Tier 2 / Tier 3 method — Joint intent + BIO slot on BERT-base.

Equivalent architecture to NVIDIA NeMo's Joint Intent & Slot
Classification model but implemented directly on HuggingFace
``transformers`` so the experiment doesn't depend on the (large)
NeMo framework.  When the experiment converges, the production
port can swap to NeMo or stay on HuggingFace — the protocol
doesn't care.

Architecture:

* Shared encoder: ``bert-base-uncased`` (or any encoder-only
  model; swap via ``--encoder``).
* Intent head: one linear layer over the ``[CLS]`` embedding
  producing logits for the 6-class intent taxonomy.
* Slot head: one linear layer over every token embedding
  producing logits for BIO slot tags (``O``, ``B-<slot>``,
  ``I-<slot>`` per slot name in the domain taxonomy).
* Joint loss: ``intent_loss + λ · slot_loss`` with ``λ=1.0``.

Training loop lives in :func:`train`; inference in :meth:`extract`.
Checkpoints are plain PyTorch ``state_dict`` dumps plus a JSON
config file — portable across machines without the NeMo runtime.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import torch
    from torch import nn
    from transformers import AutoModel, AutoTokenizer
except ImportError as exc:  # pragma: no cover — experiment-only dep
    raise RuntimeError(
        "joint_bert requires torch + transformers"
    ) from exc

from experiments.intent_slot_distillation.methods.base import (
    IntentSlotExtractor,
)
from experiments.intent_slot_distillation.schemas import (
    INTENT_CATEGORIES,
    SLOT_TAXONOMY,
    Domain,
    ExtractedLabel,
    GoldExample,
    Intent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class JointConfig:
    """Checkpoint metadata — serialised alongside the weights."""

    encoder: str = "bert-base-uncased"
    domain: Domain = "conversational"
    intent_labels: list[str] = field(default_factory=list)
    slot_labels: list[str] = field(default_factory=list)
    max_length: int = 128

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> "JointConfig":
        data = json.loads(path.read_text())
        return cls(**data)


def _build_slot_labels(domain: Domain) -> list[str]:
    """BIO tag list for a domain's slot taxonomy.

    ``O`` + ``B-<slot>`` + ``I-<slot>`` for every slot name in the
    taxonomy (plus ``object`` which is domain-common).
    """
    slots = list(SLOT_TAXONOMY[domain]) + ["object"]
    labels: list[str] = ["O"]
    for slot in slots:
        labels.append(f"B-{slot}")
        labels.append(f"I-{slot}")
    return labels


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class JointIntentSlotModel(nn.Module):
    """Encoder + intent head + slot head.

    Kept as a thin wrapper around ``transformers`` so checkpoints
    are portable and training is standard-issue HuggingFace.
    """

    def __init__(
        self,
        encoder_name: str,
        n_intents: int,
        n_slots: int,
    ) -> None:
        super().__init__()
        self.encoder = AutoModel.from_pretrained(encoder_name)
        hidden = self.encoder.config.hidden_size
        self.intent_head = nn.Linear(hidden, n_intents)
        self.slot_head = nn.Linear(hidden, n_slots)

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        output = self.encoder(
            input_ids=input_ids, attention_mask=attention_mask,
        )
        sequence = output.last_hidden_state        # (B, L, H)
        pooled = sequence[:, 0, :]                  # [CLS]
        intent_logits = self.intent_head(pooled)    # (B, n_intents)
        slot_logits = self.slot_head(sequence)      # (B, L, n_slots)
        return intent_logits, slot_logits


# ---------------------------------------------------------------------------
# Dataset helpers
# ---------------------------------------------------------------------------


def _bio_tags_for_example(
    text: str,
    slots: dict[str, str],
    tokenizer,
    slot_labels: list[str],
    max_length: int,
) -> tuple[list[int], list[int]]:
    """Produce ``(input_ids, bio_tag_ids)`` aligned to the tokenizer.

    Uses greedy surface-form matching per slot.  A production-grade
    pipeline would align via char-span offsets from the tokenizer
    (``return_offsets_mapping=True``); this experiment keeps it
    simple — if greedy match fails we label the whole example ``O``.
    """
    encoded = tokenizer(
        text,
        padding="max_length",
        truncation=True,
        max_length=max_length,
        return_offsets_mapping=True,
    )
    input_ids: list[int] = encoded["input_ids"]
    offsets: list[tuple[int, int]] = encoded["offset_mapping"]

    label_to_id = {label: i for i, label in enumerate(slot_labels)}
    tags = [label_to_id["O"]] * len(input_ids)

    text_lower = text.lower()
    for slot_name, surface in slots.items():
        if not surface:
            continue
        needle = surface.lower()
        start = text_lower.find(needle)
        if start < 0:
            continue
        end = start + len(needle)
        first = True
        for idx, (tok_start, tok_end) in enumerate(offsets):
            if tok_start == 0 and tok_end == 0:
                continue  # special token
            if tok_end <= start or tok_start >= end:
                continue
            prefix = "B-" if first else "I-"
            key = f"{prefix}{slot_name}"
            tag_id = label_to_id.get(key)
            if tag_id is None:
                continue
            tags[idx] = tag_id
            first = False
    return input_ids, tags


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def train(
    examples: list[GoldExample],
    *,
    domain: Domain,
    encoder: str = "bert-base-uncased",
    epochs: int = 3,
    batch_size: int = 16,
    learning_rate: float = 3e-5,
    checkpoint_dir: Path,
    device: str | None = None,
) -> JointConfig:
    """Fine-tune the joint model on ``examples`` and dump a checkpoint.

    ``examples`` must all share the same ``domain`` (caller's
    responsibility).  Writes:

    * ``checkpoint_dir/model.pt`` — PyTorch state dict
    * ``checkpoint_dir/config.json`` — label vocab + encoder name

    Returns the :class:`JointConfig` written to disk (useful for
    chained scripts).
    """
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")

    slot_labels = _build_slot_labels(domain)
    intent_labels = list(INTENT_CATEGORIES)
    config = JointConfig(
        encoder=encoder,
        domain=domain,
        intent_labels=intent_labels,
        slot_labels=slot_labels,
    )

    tokenizer = AutoTokenizer.from_pretrained(encoder, use_fast=True)
    model = JointIntentSlotModel(
        encoder, len(intent_labels), len(slot_labels),
    ).to(device)

    # Build tensor dataset — kept resident in memory for small runs.
    all_ids: list[list[int]] = []
    all_tags: list[list[int]] = []
    all_intents: list[int] = []
    for ex in examples:
        if ex.domain != domain:
            continue
        input_ids, tags = _bio_tags_for_example(
            ex.text, ex.slots, tokenizer, slot_labels, config.max_length,
        )
        all_ids.append(input_ids)
        all_tags.append(tags)
        all_intents.append(intent_labels.index(ex.intent))

    if not all_ids:
        raise RuntimeError(f"no examples for domain {domain!r}")

    ids_t = torch.tensor(all_ids, dtype=torch.long, device=device)
    mask_t = (ids_t != tokenizer.pad_token_id).long()
    tags_t = torch.tensor(all_tags, dtype=torch.long, device=device)
    intents_t = torch.tensor(all_intents, dtype=torch.long, device=device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    intent_loss_fn = nn.CrossEntropyLoss()
    slot_loss_fn = nn.CrossEntropyLoss()

    n = ids_t.size(0)
    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        perm = torch.randperm(n, device=device)
        for start in range(0, n, batch_size):
            batch_idx = perm[start:start + batch_size]
            batch_ids = ids_t[batch_idx]
            batch_mask = mask_t[batch_idx]
            batch_tags = tags_t[batch_idx]
            batch_intents = intents_t[batch_idx]
            intent_logits, slot_logits = model(batch_ids, batch_mask)
            intent_loss = intent_loss_fn(intent_logits, batch_intents)
            slot_loss = slot_loss_fn(
                slot_logits.reshape(-1, slot_logits.size(-1)),
                batch_tags.reshape(-1),
            )
            loss = intent_loss + slot_loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item()) * batch_ids.size(0)
        logger.info(
            "[joint-bert] epoch %d/%d loss=%.4f",
            epoch + 1, epochs, total_loss / n,
        )

    torch.save(model.state_dict(), checkpoint_dir / "model.pt")
    config.save(checkpoint_dir / "config.json")
    logger.info("[joint-bert] checkpoint written to %s", checkpoint_dir)
    return config


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class JointBert(IntentSlotExtractor):
    """Inference wrapper around a trained Joint BERT checkpoint."""

    name = "joint_bert"

    def __init__(
        self,
        checkpoint_dir: Path,
        *,
        device: str | None = None,
    ) -> None:
        self._config = JointConfig.load(checkpoint_dir / "config.json")
        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._tokenizer = AutoTokenizer.from_pretrained(
            self._config.encoder, use_fast=True,
        )
        self._model = JointIntentSlotModel(
            self._config.encoder,
            len(self._config.intent_labels),
            len(self._config.slot_labels),
        )
        state = torch.load(
            checkpoint_dir / "model.pt", map_location=self._device,
        )
        self._model.load_state_dict(state)
        self._model.to(self._device).eval()

    def extract(
        self, text: str, *, domain: Domain,
    ) -> ExtractedLabel:
        if domain != self._config.domain:
            logger.debug(
                "[joint-bert] cross-domain call: checkpoint=%s request=%s",
                self._config.domain, domain,
            )
        encoded = self._tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=self._config.max_length,
            return_offsets_mapping=True,
            return_tensors="pt",
        )
        input_ids = encoded["input_ids"].to(self._device)
        mask = encoded["attention_mask"].to(self._device)
        offsets = encoded["offset_mapping"][0].tolist()
        with torch.no_grad():
            intent_logits, slot_logits = self._model(input_ids, mask)
        intent_probs = torch.softmax(intent_logits, dim=-1)[0]
        intent_idx = int(torch.argmax(intent_probs).item())
        intent: Intent = self._config.intent_labels[intent_idx]  # type: ignore[assignment]
        intent_conf = float(intent_probs[intent_idx].item())

        slot_preds = torch.argmax(slot_logits[0], dim=-1).tolist()
        slots = self._assemble_slots(text, offsets, slot_preds)
        return ExtractedLabel(
            intent=intent,
            intent_confidence=intent_conf,
            slots=slots,
            method=self.name,
        )

    def _assemble_slots(
        self,
        text: str,
        offsets: list[list[int]],
        tag_ids: list[int],
    ) -> dict[str, str]:
        """Walk BIO tags and reconstruct surface-form slot values."""
        slots: dict[str, str] = {}
        cur_slot: str | None = None
        cur_start: int | None = None
        cur_end: int | None = None

        def _flush() -> None:
            nonlocal cur_slot, cur_start, cur_end
            if cur_slot and cur_start is not None and cur_end is not None:
                slots.setdefault(cur_slot, text[cur_start:cur_end].strip())
            cur_slot = None
            cur_start = None
            cur_end = None

        for (tok_start, tok_end), tag_id in zip(offsets, tag_ids, strict=False):
            label = self._config.slot_labels[tag_id]
            if label == "O" or (tok_start == 0 and tok_end == 0):
                _flush()
                continue
            prefix, _, slot_name = label.partition("-")
            if prefix == "B":
                _flush()
                cur_slot = slot_name
                cur_start = tok_start
                cur_end = tok_end
            elif prefix == "I" and slot_name == cur_slot:
                cur_end = tok_end
            else:
                _flush()
        _flush()
        return slots


__all__ = ["JointBert", "JointConfig", "train"]
