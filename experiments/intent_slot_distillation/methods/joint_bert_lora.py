"""Tier 3 — LoRA adapter + multi-head Joint BERT (Sprint 1 + Sprint 2).

Architecture::

    bert-base-uncased            ← frozen at production
      └── LoRA adapter           ← 10-15 MB, per deployment
             ├── intent_head     ← preference: 6 classes
             ├── topic_head      ← domain taxonomy (variable size)
             ├── admission_head  ← persist / ephemeral / discard
             ├── state_change    ← declaration / retirement / none
             └── slot_head       ← BIO tags, domain-specific

The encoder is shared across all heads (one forward pass →
six classification decisions), and LoRA keeps the trainable
parameter count small enough to ship as a single artifact per
deployment (vs. ~400 MB for full fine-tuning).

Per-example multi-head label masking: if a row only carries
``intent`` + ``slots`` (the original corpus schema), the topic /
admission / state_change heads skip loss contribution on that row.
New corpora can be labelled incrementally without re-flowing
existing data.

Artifact format::

    adapters/<domain>/<version>/
      ├── lora_adapter/        ← peft save_pretrained dir
      ├── heads.safetensors    ← intent/topic/admission/state/slot heads
      ├── manifest.json        ← encoder, label vocabs, train metrics
      ├── taxonomy.yaml        ← human-readable label vocab snapshot
      └── eval_report.md       ← gate metrics at promotion time
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import torch
    from peft import LoraConfig, get_peft_model
    from safetensors.torch import load_file as load_safetensors
    from safetensors.torch import save_file as save_safetensors
    from torch import nn
    from transformers import AutoModel, AutoTokenizer
except ImportError as exc:  # pragma: no cover — experiment-only dep
    raise RuntimeError(
        "joint_bert_lora requires torch + transformers + peft + safetensors",
    ) from exc

from experiments.intent_slot_distillation.methods.base import (
    IntentSlotExtractor,
)
from experiments.intent_slot_distillation.schemas import (
    ADMISSION_DECISIONS,
    INTENT_CATEGORIES,
    SLOT_TAXONOMY,
    STATE_CHANGES,
    Domain,
    ExtractedLabel,
    GoldExample,
)

logger = logging.getLogger(__name__)


def _pick_device() -> str:
    """Resolve best device (CUDA > MPS > CPU).

    Delegates to the shared NCMS hardware resolver when the package
    is importable so the experiment respects ``NCMS_DEVICE`` /
    ``NCMS_JOINT_BERT_DEVICE`` overrides.  Falls back to an inline
    check when run outside the NCMS checkout.
    """
    try:
        from ncms.infrastructure.hardware import resolve_device
        return resolve_device("NCMS_JOINT_BERT_DEVICE")
    except ImportError:
        pass
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------------------
# Manifest + taxonomy
# ---------------------------------------------------------------------------


@dataclass
class AdapterManifest:
    """Persisted alongside every adapter artifact.

    Captures the exact labels the adapter was trained on so inference
    code can materialise the right head shapes, plus enough provenance
    to gate promotions against prior versions.
    """

    encoder: str = "bert-base-uncased"
    domain: Domain = "conversational"
    version: str = "v1"
    max_length: int = 128

    intent_labels: list[str] = field(default_factory=list)
    slot_labels: list[str] = field(default_factory=list)
    topic_labels: list[str] = field(default_factory=list)
    admission_labels: list[str] = field(default_factory=list)
    state_change_labels: list[str] = field(default_factory=list)

    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_target_modules: list[str] = field(
        default_factory=lambda: ["query", "value"],
    )

    trained_on: dict[str, int] = field(default_factory=dict)
    gate_metrics: dict[str, float] = field(default_factory=dict)
    trained_at: str = ""
    corpus_hash: str = ""

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> AdapterManifest:
        return cls(**json.loads(path.read_text()))


def _build_slot_labels(domain: Domain) -> list[str]:
    """BIO tag list for a domain's slot taxonomy.

    ``O`` + ``B-<slot>`` + ``I-<slot>`` per slot name.  ``object`` is
    added as a domain-common catch-all so conversational gold
    round-trips.
    """
    slots = list(SLOT_TAXONOMY[domain]) + ["object"]
    labels: list[str] = ["O"]
    for slot in slots:
        labels.append(f"B-{slot}")
        labels.append(f"I-{slot}")
    # Dedupe while preserving order — ``object`` may already be in
    # SLOT_TAXONOMY for conversational domain.
    seen: set[str] = set()
    deduped: list[str] = []
    for label in labels:
        if label not in seen:
            seen.add(label)
            deduped.append(label)
    return deduped


# ---------------------------------------------------------------------------
# Multi-head model
# ---------------------------------------------------------------------------


class LoraJointModel(nn.Module):
    """BERT (or BERT+LoRA) + five classification heads.

    Construction is two-step so training and inference share code
    without peft double-wrapping.  Training path builds the heads +
    raw encoder, then calls :meth:`wrap_encoder_with_lora`; inference
    path builds the heads + raw encoder, then replaces
    ``self.encoder`` with a ``PeftModel.from_pretrained(...)``.
    """

    def __init__(
        self,
        encoder_name: str,
        n_intents: int,
        n_slots: int,
        n_topics: int,
        n_admission: int,
        n_state_change: int,
    ) -> None:
        super().__init__()
        self.encoder = AutoModel.from_pretrained(encoder_name)
        hidden = self.encoder.config.hidden_size

        self.intent_head = nn.Linear(hidden, n_intents)
        self.slot_head = nn.Linear(hidden, n_slots)
        # Topic / admission / state-change heads are constructed even
        # when the taxonomy is empty (n=0) to keep the forward signature
        # stable; training code masks unlabeled heads per-example.
        self.topic_head = nn.Linear(hidden, max(n_topics, 1))
        self.admission_head = nn.Linear(hidden, max(n_admission, 1))
        self.state_change_head = nn.Linear(hidden, max(n_state_change, 1))

    def wrap_encoder_with_lora(
        self,
        *,
        lora_r: int = 8,
        lora_alpha: int = 16,
        lora_dropout: float = 0.05,
        lora_target_modules: list[str] | None = None,
    ) -> None:
        """Replace ``self.encoder`` with a fresh peft LoRA wrapper.

        Called from the training path only.  Inference uses
        :func:`peft.PeftModel.from_pretrained` to load the saved
        adapter directly onto the raw encoder, avoiding the
        ``multiple adapters`` warning from peft.
        """
        lora_cfg = LoraConfig(
            r=lora_r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            bias="none",
            target_modules=lora_target_modules or ["query", "value"],
        )
        self.encoder = get_peft_model(self.encoder, lora_cfg)

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        out = self.encoder(
            input_ids=input_ids, attention_mask=attention_mask,
        )
        sequence = out.last_hidden_state       # (B, L, H)
        pooled = sequence[:, 0, :]              # [CLS]
        return {
            "intent": self.intent_head(pooled),
            "topic": self.topic_head(pooled),
            "admission": self.admission_head(pooled),
            "state_change": self.state_change_head(pooled),
            "slot": self.slot_head(sequence),
        }

    def save_heads(self, path: Path) -> None:
        """Dump the five heads to a single safetensors file."""
        state = {f"{k}.{sk}": v for k, sv in [
            ("intent_head", dict(self.intent_head.state_dict())),
            ("slot_head", dict(self.slot_head.state_dict())),
            ("topic_head", dict(self.topic_head.state_dict())),
            ("admission_head", dict(self.admission_head.state_dict())),
            ("state_change_head", dict(self.state_change_head.state_dict())),
        ] for sk, v in sv.items()}
        save_safetensors(state, str(path))

    def load_heads(self, path: Path) -> None:
        """Restore the five heads from safetensors.

        Tolerates shape mismatches where the checkpoint has a ``n=1``
        placeholder head and the current instance was constructed with
        the real taxonomy (or vice versa) — skips the mismatch so
        callers can wire the right head later.
        """
        state = load_safetensors(str(path))
        for key, tensor in state.items():
            head, _, param = key.partition(".")
            module = getattr(self, head, None)
            if module is None:
                logger.warning("[lora] unknown head %s in checkpoint", head)
                continue
            current = module.state_dict().get(param)
            if current is None:
                logger.warning("[lora] missing param %s on %s", param, head)
                continue
            if current.shape != tensor.shape:
                logger.warning(
                    "[lora] shape mismatch on %s.%s: ckpt=%s current=%s — "
                    "skipping",
                    head, param, tuple(tensor.shape), tuple(current.shape),
                )
                continue
            current.copy_(tensor)


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

    Pad / special-token positions get tag ``-100`` so
    :class:`CrossEntropyLoss` ignores them; real content tokens get
    ``O`` unless covered by a slot surface-form match.  This mirrors
    the mask convention from :mod:`.joint_bert`.
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
    tags: list[int] = [-100] * len(input_ids)
    for idx, (tok_start, tok_end) in enumerate(offsets):
        if tok_start == 0 and tok_end == 0:
            continue
        tags[idx] = label_to_id["O"]

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
                continue
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
    adapter_dir: Path,
    manifest: AdapterManifest,
    epochs: int = 5,
    batch_size: int = 16,
    learning_rate: float = 3e-4,
    device: str | None = None,
    slot_non_o_weight: float = 5.0,
) -> AdapterManifest:
    """Fine-tune the LoRA adapter + heads on ``examples``.

    ``manifest`` is the source of truth for head sizes + label
    vocabularies; the caller composes it from the corpus + taxonomy
    files before calling in.  This function updates
    ``manifest.gate_metrics`` and ``manifest.trained_on`` in place and
    writes the full artifact to ``adapter_dir``.

    Multi-head loss composition:

    * intent, topic, admission, state_change — cross-entropy; skipped
      per-example when the label is ``None`` (per-head mask).
    * slot — class-weighted cross-entropy with ``ignore_index=-100``
      (same loss-masking convention as the full-FT baseline).
    * Total loss = sum of non-skipped heads.  Unweighted for now —
      balancing knob is a Sprint 2 follow-up if any single head
      dominates in practice.
    """
    adapter_dir.mkdir(parents=True, exist_ok=True)
    device = device or _pick_device()

    slot_labels = manifest.slot_labels
    intent_labels = manifest.intent_labels
    topic_labels = manifest.topic_labels
    admission_labels = manifest.admission_labels
    state_change_labels = manifest.state_change_labels

    tokenizer = AutoTokenizer.from_pretrained(manifest.encoder, use_fast=True)
    model = LoraJointModel(
        encoder_name=manifest.encoder,
        n_intents=len(intent_labels),
        n_slots=len(slot_labels),
        n_topics=len(topic_labels),
        n_admission=len(admission_labels),
        n_state_change=len(state_change_labels),
    )
    model.wrap_encoder_with_lora(
        lora_r=manifest.lora_r,
        lora_alpha=manifest.lora_alpha,
        lora_dropout=manifest.lora_dropout,
        lora_target_modules=manifest.lora_target_modules,
    )
    model = model.to(device)

    intent_idx = {label: i for i, label in enumerate(intent_labels)}
    topic_idx = {label: i for i, label in enumerate(topic_labels)}
    admission_idx = {label: i for i, label in enumerate(admission_labels)}
    state_idx = {label: i for i, label in enumerate(state_change_labels)}

    # Build tensor dataset (small enough to stay resident).
    ids_rows: list[list[int]] = []
    tag_rows: list[list[int]] = []
    intents: list[int] = []
    topics: list[int] = []
    admissions: list[int] = []
    state_changes: list[int] = []
    topic_mask: list[int] = []
    admission_mask: list[int] = []
    state_change_mask: list[int] = []

    for ex in examples:
        if ex.domain != domain:
            continue
        input_ids, tags = _bio_tags_for_example(
            ex.text, ex.slots, tokenizer, slot_labels, manifest.max_length,
        )
        ids_rows.append(input_ids)
        tag_rows.append(tags)
        intents.append(intent_idx[ex.intent])
        # Per-head label masks: -100 → skipped by CrossEntropyLoss.
        if ex.topic is not None and ex.topic in topic_idx:
            topics.append(topic_idx[ex.topic])
            topic_mask.append(1)
        else:
            topics.append(0)
            topic_mask.append(0)
        if ex.admission is not None and ex.admission in admission_idx:
            admissions.append(admission_idx[ex.admission])
            admission_mask.append(1)
        else:
            admissions.append(0)
            admission_mask.append(0)
        if (
            ex.state_change is not None
            and ex.state_change in state_idx
        ):
            state_changes.append(state_idx[ex.state_change])
            state_change_mask.append(1)
        else:
            state_changes.append(0)
            state_change_mask.append(0)

    if not ids_rows:
        raise RuntimeError(f"no examples for domain {domain!r}")

    ids_t = torch.tensor(ids_rows, dtype=torch.long, device=device)
    mask_t = (ids_t != tokenizer.pad_token_id).long()
    tags_t = torch.tensor(tag_rows, dtype=torch.long, device=device)
    intents_t = torch.tensor(intents, dtype=torch.long, device=device)
    topics_t = torch.tensor(topics, dtype=torch.long, device=device)
    admissions_t = torch.tensor(admissions, dtype=torch.long, device=device)
    state_changes_t = torch.tensor(
        state_changes, dtype=torch.long, device=device,
    )
    topic_mask_t = torch.tensor(topic_mask, dtype=torch.bool, device=device)
    admission_mask_t = torch.tensor(
        admission_mask, dtype=torch.bool, device=device,
    )
    state_change_mask_t = torch.tensor(
        state_change_mask, dtype=torch.bool, device=device,
    )

    # Class-weighted slot loss — non-O tags upweighted to counter
    # ~100:1 token imbalance on pre-pad content.  Same convention as
    # the full-FT baseline in :mod:`.joint_bert`.
    slot_weights = torch.ones(len(slot_labels), device=device)
    for i, label in enumerate(slot_labels):
        if label != "O":
            slot_weights[i] = slot_non_o_weight
    slot_loss_fn = nn.CrossEntropyLoss(
        weight=slot_weights, ignore_index=-100,
    )
    intent_loss_fn = nn.CrossEntropyLoss()
    head_loss_fn = nn.CrossEntropyLoss(reduction="none")

    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=learning_rate,
    )

    n = ids_t.size(0)
    model.train()
    final_avg_loss = float("nan")
    for epoch in range(epochs):
        total_loss = 0.0
        perm = torch.randperm(n, device=device)
        for start in range(0, n, batch_size):
            batch_idx = perm[start:start + batch_size]
            logits = model(ids_t[batch_idx], mask_t[batch_idx])

            intent_loss = intent_loss_fn(
                logits["intent"], intents_t[batch_idx],
            )
            slot_loss = slot_loss_fn(
                logits["slot"].reshape(-1, logits["slot"].size(-1)),
                tags_t[batch_idx].reshape(-1),
            )

            # Per-head masked losses — mean only over rows that carry
            # that head's label.
            topic_per = head_loss_fn(
                logits["topic"][:, :len(topic_labels) or 1],
                topics_t[batch_idx],
            )
            topic_m = topic_mask_t[batch_idx].float()
            topic_loss = (
                (topic_per * topic_m).sum() / (topic_m.sum() + 1e-9)
                if topic_m.sum() > 0 else torch.tensor(0.0, device=device)
            )
            admit_per = head_loss_fn(
                logits["admission"][:, :len(admission_labels) or 1],
                admissions_t[batch_idx],
            )
            admit_m = admission_mask_t[batch_idx].float()
            admit_loss = (
                (admit_per * admit_m).sum() / (admit_m.sum() + 1e-9)
                if admit_m.sum() > 0 else torch.tensor(0.0, device=device)
            )
            state_per = head_loss_fn(
                logits["state_change"][:, :len(state_change_labels) or 1],
                state_changes_t[batch_idx],
            )
            state_m = state_change_mask_t[batch_idx].float()
            state_loss = (
                (state_per * state_m).sum() / (state_m.sum() + 1e-9)
                if state_m.sum() > 0 else torch.tensor(0.0, device=device)
            )

            loss = (
                intent_loss + slot_loss
                + topic_loss + admit_loss + state_loss
            )
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item()) * batch_idx.size(0)
        avg = total_loss / n
        final_avg_loss = avg
        logger.info(
            "[lora] epoch %d/%d loss=%.4f", epoch + 1, epochs, avg,
        )

    # Save adapter + heads + manifest.
    model.encoder.save_pretrained(str(adapter_dir / "lora_adapter"))
    model.save_heads(adapter_dir / "heads.safetensors")
    manifest.trained_on = {"n_examples": n, "epochs": epochs}
    manifest.gate_metrics = {
        **manifest.gate_metrics,
        "final_train_loss": round(final_avg_loss, 4),
    }
    manifest.save(adapter_dir / "manifest.json")

    # Human-readable taxonomy snapshot.
    try:
        import yaml
        (adapter_dir / "taxonomy.yaml").write_text(
            yaml.safe_dump({
                "intent_labels": intent_labels,
                "slot_labels": slot_labels,
                "topic_labels": topic_labels,
                "admission_labels": admission_labels,
                "state_change_labels": state_change_labels,
            }, sort_keys=False),
        )
    except ImportError:
        pass

    logger.info("[lora] adapter written to %s", adapter_dir)
    return manifest


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class LoraJointBert(IntentSlotExtractor):
    """Inference wrapper around a trained LoRA adapter artifact."""

    name = "joint_bert_lora"

    def __init__(
        self,
        adapter_dir: Path,
        *,
        device: str | None = None,
    ) -> None:
        self._manifest = AdapterManifest.load(adapter_dir / "manifest.json")
        self._device = device or _pick_device()
        logger.info("[lora] inference device: %s", self._device)

        self._tokenizer = AutoTokenizer.from_pretrained(
            self._manifest.encoder, use_fast=True,
        )

        # Build heads + raw encoder; don't pre-wrap with LoRA.
        # Replace the raw encoder with the saved adapter via
        # ``PeftModel.from_pretrained``.  This avoids the peft
        # ``multiple adapters`` warning.
        from peft import PeftModel

        self._model = LoraJointModel(
            encoder_name=self._manifest.encoder,
            n_intents=len(self._manifest.intent_labels),
            n_slots=len(self._manifest.slot_labels),
            n_topics=len(self._manifest.topic_labels),
            n_admission=len(self._manifest.admission_labels),
            n_state_change=len(self._manifest.state_change_labels),
        )
        self._model.encoder = PeftModel.from_pretrained(
            self._model.encoder, str(adapter_dir / "lora_adapter"),
        )
        self._model.load_heads(adapter_dir / "heads.safetensors")
        self._model.to(self._device).eval()

    def extract(
        self, text: str, *, domain: Domain,
    ) -> ExtractedLabel:
        if domain != self._manifest.domain:
            logger.debug(
                "[lora] cross-domain call: adapter=%s request=%s",
                self._manifest.domain, domain,
            )
        encoded = self._tokenizer(
            text,
            padding="max_length",
            truncation=True,
            max_length=self._manifest.max_length,
            return_offsets_mapping=True,
            return_tensors="pt",
        )
        input_ids = encoded["input_ids"].to(self._device)
        mask = encoded["attention_mask"].to(self._device)
        offsets = encoded["offset_mapping"][0].tolist()
        with torch.no_grad():
            logits = self._model(input_ids, mask)

        intent, intent_conf = self._argmax_one_hot(
            logits["intent"][0], self._manifest.intent_labels,
        )
        topic, topic_conf = self._argmax_one_hot(
            logits["topic"][0], self._manifest.topic_labels,
        )
        admission, admission_conf = self._argmax_one_hot(
            logits["admission"][0], self._manifest.admission_labels,
        )
        state_change, state_change_conf = self._argmax_one_hot(
            logits["state_change"][0], self._manifest.state_change_labels,
        )

        slot_preds = torch.argmax(logits["slot"][0], dim=-1).tolist()
        slots = self._assemble_slots(text, offsets, slot_preds)
        return ExtractedLabel(
            intent=intent,  # type: ignore[arg-type]
            intent_confidence=intent_conf,
            slots=slots,
            topic=topic,
            topic_confidence=topic_conf,
            admission=admission,  # type: ignore[arg-type]
            admission_confidence=admission_conf,
            state_change=state_change,  # type: ignore[arg-type]
            state_change_confidence=state_change_conf,
            method=self.name,
        )

    def _argmax_one_hot(
        self, head_logits: torch.Tensor, label_vocab: list[str],
    ) -> tuple[str | None, float | None]:
        """Softmax argmax over a single head.

        Returns ``(None, None)`` when the head has an empty vocab
        (untrained head in the adapter).  Confidence is the softmax
        probability of the winning class.
        """
        if not label_vocab:
            return None, None
        n = len(label_vocab)
        probs = torch.softmax(head_logits[:n], dim=-1)
        idx = int(torch.argmax(probs).item())
        return label_vocab[idx], float(probs[idx].item())

    def _assemble_slots(
        self,
        text: str,
        offsets: list[list[int]],
        tag_ids: list[int],
    ) -> dict[str, str]:
        """Walk BIO tags + offsets to reconstruct surface-form slot values."""
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

        for (tok_start, tok_end), tag_id in zip(
            offsets, tag_ids, strict=False,
        ):
            label = self._manifest.slot_labels[tag_id]
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


def build_manifest(
    *,
    domain: Domain,
    encoder: str = "bert-base-uncased",
    topic_labels: list[str] | None = None,
    admission_labels: list[str] | None = None,
    state_change_labels: list[str] | None = None,
    lora_r: int = 8,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    lora_target_modules: list[str] | None = None,
    max_length: int = 128,
    version: str = "v1",
) -> AdapterManifest:
    """Compose a manifest ready for :func:`train`.

    Intent + slot vocabs are fixed by the shared schemas.  Topic /
    admission / state-change vocabs come from the caller's taxonomy
    file (YAML).  Empty lists are allowed — the corresponding head
    still exists but its output is treated as "no label" at inference.
    """
    return AdapterManifest(
        encoder=encoder,
        domain=domain,
        version=version,
        max_length=max_length,
        intent_labels=list(INTENT_CATEGORIES),
        slot_labels=_build_slot_labels(domain),
        topic_labels=topic_labels or [],
        admission_labels=(
            admission_labels if admission_labels is not None
            else list(ADMISSION_DECISIONS)
        ),
        state_change_labels=(
            state_change_labels if state_change_labels is not None
            else list(STATE_CHANGES)
        ),
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        lora_target_modules=lora_target_modules or ["query", "value"],
    )


__all__ = [
    "AdapterManifest",
    "LoraJointBert",
    "LoraJointModel",
    "build_manifest",
    "train",
]
