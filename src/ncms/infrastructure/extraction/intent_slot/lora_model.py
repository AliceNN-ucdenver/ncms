"""LoRA multi-head inference model — production copy.

Self-contained LoRA+heads inference code.  Training driver lives
at :mod:`ncms.application.adapters.methods.joint_bert_lora`; this
module hosts only the nn.Module + the :class:`LoraJointBert`
inference class plus head save/load helpers.

Architecture (v7+, with v6 compat shim)::

    bert-base-uncased (raw) → wrap_encoder_with_lora()
                                    ↓ frozen at production
    LoRA adapter (10-15 MB) + heads:
       ├── intent_head     (6 classes, [CLS]-pooled)
       ├── topic_head      (domain taxonomy, [CLS]-pooled)
       ├── admission_head  (persist / ephemeral / discard)
       ├── state_change    (declaration / retirement / none)
       ├── shape_intent    (TLG grammar shape, query-voice)
       └── role_head       (v7: primary / alternative / casual /
                             not_relevant — span-pooled over
                             gazetteer-detected surfaces)
       └── slot_head       (v6 legacy: BIO tag vocabulary — only
                             instantiated when the manifest has
                             ``slot_labels`` but empty ``role_labels``)

v7 adapters: slots are reconstructed at inference from role-
classified gazetteer spans.  v6 adapters: slots are decoded from
BIO tags on the shared encoder sequence output.  One forward pass
covers both paths.  Latency on Apple Silicon MPS: ~20-65 ms p95.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

try:
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from safetensors.torch import load_file as load_safetensors
    from safetensors.torch import save_file as save_safetensors
    from torch import nn
    from transformers import AutoModel, AutoTokenizer
except ImportError as exc:  # pragma: no cover — P2 backend optional
    raise RuntimeError(
        "intent_slot.lora_model requires torch + transformers + "
        "peft + safetensors",
    ) from exc

from ncms.domain.models import ExtractedLabel
from ncms.infrastructure.extraction.intent_slot.adapter_loader import (
    AdapterManifest,
)

try:  # v7 catalog-based gazetteer — optional at runtime
    from ncms.application.adapters.sdg.catalog import detect_spans
except ImportError:  # pragma: no cover — catalog package missing
    detect_spans = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _pick_device() -> str:
    """Resolve best device (CUDA > MPS > CPU).

    Honours ``NCMS_INTENT_SLOT_DEVICE`` then ``NCMS_DEVICE`` via the
    shared hardware resolver.
    """
    try:
        from ncms.infrastructure.hardware import resolve_device
        return resolve_device("NCMS_INTENT_SLOT_DEVICE")
    except ImportError:
        pass
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------------------
# Multi-head model
# ---------------------------------------------------------------------------


class LoraJointModel(nn.Module):
    """BERT (or BERT+LoRA) + six classification heads.

    Six heads (v6+): intent, slot, topic, admission, state_change,
    shape_intent.  The last (query-shape) is the replacement for
    the hand-coded regex classifier in ``domain/tlg/query_parser.py``.

    Construction is two-step so training and inference share code
    without peft double-wrapping.  Training calls
    :meth:`wrap_encoder_with_lora` after constructing the heads;
    inference replaces ``self.encoder`` via
    :func:`peft.PeftModel.from_pretrained`.
    """

    def __init__(
        self,
        encoder_name: str,
        n_intents: int,
        n_slots: int,
        n_topics: int,
        n_admission: int,
        n_state_change: int,
        n_shape_intents: int = 0,
        n_roles: int = 0,
    ) -> None:
        super().__init__()
        self.encoder = AutoModel.from_pretrained(encoder_name)
        hidden = self.encoder.config.hidden_size

        self.intent_head = nn.Linear(hidden, n_intents)
        # Legacy BIO slot head (v6 adapters).  Instantiated even when
        # ``n_slots=0`` so ``load_heads`` can silently ignore v7
        # checkpoints where slot_head.weight is absent.
        self.slot_head = nn.Linear(hidden, max(n_slots, 1))
        # Heads are constructed even with empty vocab (n=0) to keep
        # the forward signature stable; training masks unlabeled
        # heads per-example, inference skips them if vocab empty.
        self.topic_head = nn.Linear(hidden, max(n_topics, 1))
        self.admission_head = nn.Linear(hidden, max(n_admission, 1))
        self.state_change_head = nn.Linear(hidden, max(n_state_change, 1))
        # 6th head (v6+): query-shape classifier.  ``n_shape_intents=0``
        # is the pre-v6 compatibility path — head exists but is a
        # no-op linear layer; callers ignore its output when the
        # adapter manifest's ``shape_intent_labels`` list is empty.
        self.shape_intent_head = nn.Linear(hidden, max(n_shape_intents, 1))
        # v7 role head — primary/alternative/casual/not_relevant
        # over span-pooled subword representations.  ``n_roles=0``
        # means a pre-v7 adapter; the load_heads path leaves this as
        # a random init and the inference path uses the BIO slot_head.
        self.role_head = nn.Linear(hidden, max(n_roles, 1))

    def wrap_encoder_with_lora(
        self,
        *,
        lora_r: int = 8,
        lora_alpha: int = 16,
        lora_dropout: float = 0.05,
        lora_target_modules: list[str] | None = None,
    ) -> None:
        """Replace ``self.encoder`` with a fresh LoRA wrapper.

        Called from the training path.  Inference uses
        :func:`peft.PeftModel.from_pretrained` to load a saved
        adapter onto the raw encoder — avoids the "multiple
        adapters" warning peft emits on double-wrap.
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
            "shape_intent": self.shape_intent_head(pooled),
            "slot": self.slot_head(sequence),
            # ``sequence`` is exposed so the v7 role head can pool
            # span representations without a second forward pass.
            "sequence": sequence,
        }

    def save_heads(self, path: Path) -> None:
        """Dump all heads to a single safetensors file."""
        state = {
            f"{head}.{param}": tensor
            for head, head_state in [
                ("intent_head", dict(self.intent_head.state_dict())),
                ("slot_head", dict(self.slot_head.state_dict())),
                ("role_head", dict(self.role_head.state_dict())),
                ("topic_head", dict(self.topic_head.state_dict())),
                ("admission_head", dict(self.admission_head.state_dict())),
                ("state_change_head", dict(self.state_change_head.state_dict())),
                ("shape_intent_head", dict(self.shape_intent_head.state_dict())),
            ]
            for param, tensor in head_state.items()
        }
        save_safetensors(state, str(path))

    def load_heads(self, path: Path) -> None:
        """Restore the five heads from safetensors.

        Tolerates shape mismatches (skips + logs) so a checkpoint
        with placeholder ``n=1`` heads can load onto an instance
        constructed with the real taxonomy (or vice versa).
        """
        state = load_safetensors(str(path))
        for key, tensor in state.items():
            head, _, param = key.partition(".")
            module = getattr(self, head, None)
            if module is None:
                logger.warning(
                    "[intent_slot] unknown head %s in checkpoint", head,
                )
                continue
            current = module.state_dict().get(param)
            if current is None:
                logger.warning(
                    "[intent_slot] missing param %s on %s", param, head,
                )
                continue
            if current.shape != tensor.shape:
                logger.warning(
                    "[intent_slot] shape mismatch on %s.%s: ckpt=%s "
                    "current=%s — skipping",
                    head, param, tuple(tensor.shape),
                    tuple(current.shape),
                )
                continue
            current.copy_(tensor)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class LoraJointBert:
    """Inference-only wrapper around a trained LoRA adapter.

    Returns NCMS :class:`ExtractedLabel` directly — no type
    conversion layer between this and the ingest pipeline.
    """

    name = "joint_bert_lora"

    def __init__(
        self,
        adapter_dir: Path,
        manifest: AdapterManifest,
        *,
        device: str | None = None,
    ) -> None:
        self._manifest = manifest
        self._device = device or _pick_device()
        logger.info(
            "[intent_slot] LoraJointBert inference device: %s", self._device,
        )

        self._tokenizer = AutoTokenizer.from_pretrained(
            manifest.encoder, use_fast=True,
        )

        self._model = LoraJointModel(
            encoder_name=manifest.encoder,
            n_intents=len(manifest.intent_labels),
            n_slots=len(manifest.slot_labels),
            n_topics=len(manifest.topic_labels),
            n_admission=len(manifest.admission_labels),
            n_state_change=len(manifest.state_change_labels),
            n_shape_intents=len(manifest.shape_intent_labels),
            n_roles=len(manifest.role_labels),
        )
        # Replace the raw encoder with the saved LoRA adapter.
        self._model.encoder = PeftModel.from_pretrained(
            self._model.encoder, str(adapter_dir / "lora_adapter"),
        )
        self._model.load_heads(adapter_dir / "heads.safetensors")
        self._model.to(self._device).eval()
        # v7 dispatch: role_labels populated means gazetteer + role
        # head; empty + slot_labels populated means legacy BIO path.
        self._use_role_head = bool(manifest.role_labels)
        if self._use_role_head and detect_spans is None:
            logger.warning(
                "[intent_slot] adapter manifest declares role_labels "
                "but catalog.detect_spans is unavailable; falling "
                "back to BIO slot decoding",
            )
            self._use_role_head = False

    def extract(self, text: str, *, domain: str) -> ExtractedLabel:
        if domain != self._manifest.domain:
            logger.debug(
                "[intent_slot] cross-domain call: adapter=%s request=%s",
                self._manifest.domain, domain,
            )

        t0 = time.perf_counter()
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
        # 6th head — query-shape.  Empty vocab means the adapter
        # predates the v6 schema; surface ``None`` so callers treat
        # the output as "abstain" and skip TLG routing.
        shape_intent, shape_intent_conf = self._argmax_one_hot(
            logits["shape_intent"][0], self._manifest.shape_intent_labels,
        )

        # v7: gazetteer spans + role head; v6 fallback: BIO decode.
        if self._use_role_head:
            slots = self._assemble_slots_v7(
                text, offsets, logits["sequence"][0],
            )
        else:
            slot_preds = torch.argmax(logits["slot"][0], dim=-1).tolist()
            slots = self._assemble_slots(text, offsets, slot_preds)

        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Normalise intent to the NCMS enum domain; anything outside
        # the known vocab collapses to "none" so the caller never
        # sees an invalid Literal value.
        if intent not in {
            "positive", "negative", "habitual",
            "difficulty", "choice", "none",
        }:
            intent = "none"
            intent_conf = 0.0

        # Shape-intent normalisation — anything outside the 12 + none
        # TLG taxonomy collapses to None (abstain).
        _VALID_SHAPE_INTENTS = {
            "current_state", "before_named", "concurrent", "origin",
            "retirement", "sequence", "predecessor", "transitive_cause",
            "causal_chain", "interval", "ordinal_first", "ordinal_last",
            "none",
        }
        if shape_intent not in _VALID_SHAPE_INTENTS:
            shape_intent = None
            shape_intent_conf = None

        return ExtractedLabel(
            intent=intent,  # type: ignore[arg-type]
            intent_confidence=float(intent_conf or 0.0),
            slots=slots,
            topic=topic,
            topic_confidence=topic_conf,
            admission=admission if admission in {  # type: ignore[arg-type]
                "persist", "ephemeral", "discard",
            } else None,
            admission_confidence=admission_conf,
            state_change=state_change if state_change in {  # type: ignore[arg-type]
                "declaration", "retirement", "none",
            } else None,
            state_change_confidence=state_change_conf,
            shape_intent=shape_intent,  # type: ignore[arg-type]
            shape_intent_confidence=shape_intent_conf,
            method=self.name,
            latency_ms=latency_ms,
        )

    def _argmax_one_hot(
        self, head_logits: torch.Tensor, label_vocab: list[str],
    ) -> tuple[str | None, float | None]:
        if not label_vocab:
            return None, None
        n = len(label_vocab)
        probs = torch.softmax(head_logits[:n], dim=-1)
        idx = int(torch.argmax(probs).item())
        return label_vocab[idx], float(probs[idx].item())

    def _assemble_slots_v7(
        self,
        text: str,
        offsets: list[list[int]],
        sequence_row: torch.Tensor,   # (L, H)
    ) -> dict[str, str]:
        """v7 slot assembly: gazetteer + role-head span pooling.

        1. Run ``catalog.detect_spans`` to get authoritative catalog
           hits with their canonical form + slot.
        2. For each span, build a token-mask over ``offsets`` that
           overlaps the span's character range.
        3. Mean-pool the span's subword vectors from ``sequence_row``
           and push through ``role_head`` to get the role.
        4. Populate the slots dict: primary → typed slot, alternative
           → ``alternative`` slot.  Casual / not_relevant dropped.
        """
        if detect_spans is None:
            return {}
        gaz = detect_spans(text, domain=self._manifest.domain)  # type: ignore[arg-type]
        if not gaz:
            return {}
        role_labels = self._manifest.role_labels or [
            "primary", "alternative", "casual", "not_relevant",
        ]
        max_len = self._manifest.max_length
        span_masks: list[list[float]] = []
        for s in gaz:
            mask = [0.0] * max_len
            for idx, off in enumerate(offsets):
                if idx >= max_len:
                    break
                tok_start = int(off[0])
                tok_end = int(off[1])
                if tok_start == 0 and tok_end == 0:
                    continue
                if tok_end <= s.char_start or tok_start >= s.char_end:
                    continue
                mask[idx] = 1.0
            span_masks.append(mask)

        valid = [
            (s, m) for s, m in zip(gaz, span_masks, strict=True)
            if sum(m) > 0
        ]
        if not valid:
            return {}

        masks_t = torch.tensor(
            [m for _, m in valid],
            dtype=torch.float32, device=self._device,
        )
        with torch.no_grad():
            pooled = masks_t @ sequence_row   # (S, L) @ (L, H)
            pooled = pooled / masks_t.sum(dim=1, keepdim=True).clamp(min=1e-9)
            role_logits = self._model.role_head(pooled)[:, :len(role_labels)]
            probs = torch.softmax(role_logits, dim=-1)

        # Max-confidence wins on multi-primary collisions — first-wins
        # arbitrarily discards equally-valid primary surfaces in rows
        # that compare two options side-by-side.  See
        # :meth:`ncms.application.adapters.methods.joint_bert_lora.
        # LoraJointBert._classify_spans` for the same fix upstream.
        best_primary: dict[str, tuple[float, str]] = {}
        best_alternative: tuple[float, str] | None = None
        for i, (s, _) in enumerate(valid):
            idx = int(torch.argmax(probs[i]).item())
            role = role_labels[idx]
            conf = float(probs[i][idx].item())
            if role == "primary":
                prev = best_primary.get(s.slot)
                if prev is None or conf > prev[0]:
                    best_primary[s.slot] = (conf, s.canonical)
            elif role == "alternative":
                if best_alternative is None or conf > best_alternative[0]:
                    best_alternative = (conf, s.canonical)
        slots: dict[str, str] = {
            slot: canon for slot, (_, canon) in best_primary.items()
        }
        if best_alternative is not None:
            slots["alternative"] = best_alternative[1]
        return slots

    def _assemble_slots(
        self,
        text: str,
        offsets: list[list[int]],
        tag_ids: list[int],
    ) -> dict[str, str]:
        """Legacy v6 BIO slot decoder.  Only invoked when the
        manifest does not ship ``role_labels``."""
        if not self._manifest.slot_labels:
            return {}
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


__all__ = ["LoraJointBert", "LoraJointModel", "_pick_device"]
