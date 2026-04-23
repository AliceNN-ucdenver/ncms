"""Tier 3 — LoRA adapter + multi-head Joint BERT (v7 six-head).

Architecture (v7+)::

    bert-base-uncased            ← frozen at production
      └── LoRA adapter           ← 10-15 MB, per deployment
             ├── intent_head     ← preference: 6 classes (pooled [CLS])
             ├── topic_head      ← domain taxonomy (pooled [CLS])
             ├── admission_head  ← persist / ephemeral / discard
             ├── state_change    ← declaration / retirement / none
             ├── shape_intent    ← TLG grammar shape (query-voice)
             └── role_head       ← primary / alternative / casual /
                                    not_relevant (per gazetteer span,
                                    span-pooled over subwords)

v7 architectural shift: head 6 (previously a BIO slot tagger) is
replaced by a **role classifier**.  Slot detection is now owned by
the gazetteer (:func:`ncms.application.adapters.sdg.catalog.detect_spans`)
— the authoritative 567-entry software_dev catalog beats the BIO
tagger on coverage (0.589 vs 0.464 F1 on held-out gold).  The SLM's
job is the nuance the gazetteer can't see: is this surface the
primary subject of the utterance, an alternative being rejected, a
casual mention, or irrelevant noise?  Final ``slots`` dict is
reconstructed from role-labeled spans at inference time.

Per-example multi-head label masking: if a row only carries
``intent`` + ``slots`` (legacy v6 schema), the role head derives
training targets from the slots dict via the gazetteer; topic /
admission / state_change heads skip loss contribution per-row as
before.

Artifact format::

    adapters/<domain>/<version>/
      ├── lora_adapter/        ← peft save_pretrained dir
      ├── heads.safetensors    ← 6 heads (intent/topic/admission/
      │                          state/shape/role)
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

from ncms.application.adapters.methods.base import (
    IntentSlotExtractor,
)
from ncms.application.adapters.schemas import (
    ADMISSION_DECISIONS,
    INTENT_CATEGORIES,
    ROLE_LABELS,
    STATE_CHANGES,
    DetectedSpan,
    Domain,
    ExtractedLabel,
    GoldExample,
    RoleSpan,
)
from ncms.application.adapters.sdg.catalog import detect_spans

logger = logging.getLogger(__name__)


def _pick_device() -> str:
    """Resolve best device (CUDA > MPS > CPU)."""
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
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class AdapterManifest:
    """Persisted alongside every adapter artifact.

    v7 schema shift: ``slot_labels`` (the BIO tag vocabulary) is
    replaced by ``role_labels`` (primary/alternative/casual/
    not_relevant).  Inference continues to populate
    :attr:`ExtractedLabel.slots` for downstream compatibility — the
    dict is rebuilt from role-classified spans at extract time.
    """

    encoder: str = "bert-base-uncased"
    domain: Domain = "conversational"
    version: str = "v1"
    max_length: int = 128

    intent_labels: list[str] = field(default_factory=list)
    # v7: role labels (4 categories) replace the BIO slot tag list.
    role_labels: list[str] = field(default_factory=list)
    topic_labels: list[str] = field(default_factory=list)
    admission_labels: list[str] = field(default_factory=list)
    state_change_labels: list[str] = field(default_factory=list)
    shape_intent_labels: list[str] = field(default_factory=list)

    # Legacy field — kept on the dataclass for serialisation round-trip
    # with v6 checkpoints.  New v7 artifacts carry an empty list here;
    # loaders that see a populated ``slot_labels`` + empty
    # ``role_labels`` know they're reading a pre-v7 adapter and can
    # handle it (or refuse to load).
    slot_labels: list[str] = field(default_factory=list)

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


# ---------------------------------------------------------------------------
# Multi-head model
# ---------------------------------------------------------------------------


class LoraJointModel(nn.Module):
    """BERT (or BERT+LoRA) + six classification heads (v7).

    Heads: intent, topic, admission, state_change, shape_intent,
    role.  The first five consume the pooled [CLS] representation;
    the role head consumes span-pooled subword representations
    (mean-pooled over the tokens that cover a gazetteer span).

    Construction is two-step so training and inference share code
    without peft double-wrapping.
    """

    def __init__(
        self,
        encoder_name: str,
        n_intents: int,
        n_roles: int,
        n_topics: int,
        n_admission: int,
        n_state_change: int,
        n_shape_intents: int = 0,
    ) -> None:
        super().__init__()
        self.encoder = AutoModel.from_pretrained(encoder_name)
        hidden = self.encoder.config.hidden_size

        self.intent_head = nn.Linear(hidden, n_intents)
        # v7 role head — 4 classes, consumes span-pooled representation.
        self.role_head = nn.Linear(hidden, max(n_roles, 1))
        self.topic_head = nn.Linear(hidden, max(n_topics, 1))
        self.admission_head = nn.Linear(hidden, max(n_admission, 1))
        self.state_change_head = nn.Linear(hidden, max(n_state_change, 1))
        self.shape_intent_head = nn.Linear(hidden, max(n_shape_intents, 1))

    def wrap_encoder_with_lora(
        self,
        *,
        lora_r: int = 8,
        lora_alpha: int = 16,
        lora_dropout: float = 0.05,
        lora_target_modules: list[str] | None = None,
    ) -> None:
        """Replace ``self.encoder`` with a fresh peft LoRA wrapper."""
        lora_cfg = LoraConfig(
            r=lora_r,
            lora_alpha=lora_alpha,
            lora_dropout=lora_dropout,
            bias="none",
            target_modules=lora_target_modules or ["query", "value"],
        )
        self.encoder = get_peft_model(self.encoder, lora_cfg)

    def encode(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Run the shared encoder; return ``(sequence, pooled)``.

        ``sequence`` is (B, L, H) — needed by the role head for
        span pooling.  ``pooled`` is (B, H) — needed by the other
        five heads.
        """
        out = self.encoder(
            input_ids=input_ids, attention_mask=attention_mask,
        )
        sequence = out.last_hidden_state       # (B, L, H)
        pooled = sequence[:, 0, :]              # [CLS]
        return sequence, pooled

    def classify_pooled(
        self, pooled: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        """Apply the 5 [CLS]-pooled heads."""
        return {
            "intent": self.intent_head(pooled),
            "topic": self.topic_head(pooled),
            "admission": self.admission_head(pooled),
            "state_change": self.state_change_head(pooled),
            "shape_intent": self.shape_intent_head(pooled),
        }

    def classify_roles(
        self, span_vectors: torch.Tensor,
    ) -> torch.Tensor:
        """Apply the role head to span-pooled vectors.  Input (S, H);
        output (S, n_roles)."""
        return self.role_head(span_vectors)

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        """Minimal forward for pooled heads only.

        The role head needs explicit span info and is therefore
        invoked through :meth:`classify_roles` from the train/infer
        paths rather than from this forward.  This signature mirrors
        the v6 interface so unrelated callers don't break.
        """
        sequence, pooled = self.encode(input_ids, attention_mask)
        logits = self.classify_pooled(pooled)
        logits["sequence"] = sequence
        return logits

    def save_heads(self, path: Path) -> None:
        """Dump the six heads to a single safetensors file."""
        state = {f"{k}.{sk}": v for k, sv in [
            ("intent_head", dict(self.intent_head.state_dict())),
            ("role_head", dict(self.role_head.state_dict())),
            ("topic_head", dict(self.topic_head.state_dict())),
            ("admission_head", dict(self.admission_head.state_dict())),
            ("state_change_head", dict(self.state_change_head.state_dict())),
            ("shape_intent_head", dict(self.shape_intent_head.state_dict())),
        ] for sk, v in sv.items()}
        save_safetensors(state, str(path))

    def load_heads(self, path: Path) -> None:
        """Restore the heads from safetensors.  Tolerates shape
        mismatches (e.g. loading v6 adapter which had slot_head
        instead of role_head) — skips the mismatch so callers can
        wire the right head later.
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
# Role span derivation (legacy gold bootstrap)
# ---------------------------------------------------------------------------


def derive_role_spans_from_slots(
    text: str, slots: dict[str, str], domain: Domain,
) -> list[RoleSpan]:
    """Bootstrap ``role_spans`` from a legacy v6 GoldExample.

    Legacy corpora have a ``slots`` dict but no per-span roles.  To
    train the role head on them, we run the gazetteer to get
    candidate spans, then label each span:

      - ``primary`` — span.canonical matches a non-alternative entry
        in ``slots``, i.e. ``slots[span.slot] == span.canonical``.
      - ``alternative`` — span.canonical matches ``slots["alternative"]``
        regardless of its catalog slot (so "MongoDB" detected as
        database but labeled alternative in gold stays alternative).
      - ``not_relevant`` — detected but not referenced in slots at all
        (distractor or casual mention the labeller skipped).

    No ``casual`` label is emitted from this derivation — legacy gold
    didn't distinguish casual mentions from genuinely unrelated
    detections.  The SDG + LLM labeler paths will emit ``casual``
    directly when they know.
    """
    gazetteer_spans = detect_spans(text, domain=domain)
    # Normalize slot values for comparison.
    slot_values_lower: dict[str, str] = {
        k: v.lower().strip() for k, v in slots.items() if v
    }
    alt_value = slot_values_lower.get("alternative")
    out: list[RoleSpan] = []
    for s in gazetteer_spans:
        canon_lower = s.canonical.lower()
        role: str
        if alt_value is not None and canon_lower == alt_value:
            role = "alternative"
        elif slot_values_lower.get(s.slot) == canon_lower:
            role = "primary"
        else:
            role = "not_relevant"
        out.append(RoleSpan(
            char_start=s.char_start,
            char_end=s.char_end,
            surface=s.surface,
            canonical=s.canonical,
            slot=s.slot,
            role=role,  # type: ignore[arg-type]
            source="derived-from-slots",
        ))
    return out


# ---------------------------------------------------------------------------
# Dataset helpers — per-span role batching
# ---------------------------------------------------------------------------


def _char_span_to_token_mask(
    offsets: list[tuple[int, int]],
    char_start: int,
    char_end: int,
    max_length: int,
) -> list[float]:
    """Return a 0/1 float mask of length ``max_length`` selecting the
    subword tokens whose character offsets overlap [char_start, char_end).

    Special tokens ([CLS], [SEP], padding) have offset (0,0) and are
    never selected.  When a span fell outside the truncated window
    (every overlap would be empty) the mask is all-zeros — callers
    detect and skip that span.
    """
    mask: list[float] = [0.0] * max_length
    for idx, (tok_start, tok_end) in enumerate(offsets):
        if idx >= max_length:
            break
        if tok_start == 0 and tok_end == 0:
            continue
        if tok_end <= char_start or tok_start >= char_end:
            continue
        mask[idx] = 1.0
    return mask


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
) -> AdapterManifest:
    """Fine-tune the LoRA adapter + heads on ``examples`` (v7).

    Per-head loss:
      * intent, topic, admission, state_change, shape_intent —
        cross-entropy, skipped per-example when the label is ``None``
        (per-head mask).
      * role — cross-entropy over span-pooled representations.  Rows
        with zero role_spans (after derivation fallback) contribute
        nothing to the role loss.
      * Total loss = sum of non-skipped heads, unweighted.

    v6 rows that only carry ``slots`` (no explicit ``role_spans``)
    are auto-labeled via :func:`derive_role_spans_from_slots`.
    """
    adapter_dir.mkdir(parents=True, exist_ok=True)
    device = device or _pick_device()

    role_labels = manifest.role_labels or list(ROLE_LABELS)
    intent_labels = manifest.intent_labels
    topic_labels = manifest.topic_labels
    admission_labels = manifest.admission_labels
    state_change_labels = manifest.state_change_labels
    shape_intent_labels = manifest.shape_intent_labels

    tokenizer = AutoTokenizer.from_pretrained(manifest.encoder, use_fast=True)
    model = LoraJointModel(
        encoder_name=manifest.encoder,
        n_intents=len(intent_labels),
        n_roles=len(role_labels),
        n_topics=len(topic_labels),
        n_admission=len(admission_labels),
        n_state_change=len(state_change_labels),
        n_shape_intents=len(shape_intent_labels),
    )
    model.wrap_encoder_with_lora(
        lora_r=manifest.lora_r,
        lora_alpha=manifest.lora_alpha,
        lora_dropout=manifest.lora_dropout,
        lora_target_modules=manifest.lora_target_modules,
    )
    model = model.to(device)

    intent_idx = {label: i for i, label in enumerate(intent_labels)}
    role_idx = {label: i for i, label in enumerate(role_labels)}
    topic_idx = {label: i for i, label in enumerate(topic_labels)}
    admission_idx = {label: i for i, label in enumerate(admission_labels)}
    state_idx = {label: i for i, label in enumerate(state_change_labels)}
    shape_idx = {label: i for i, label in enumerate(shape_intent_labels)}

    # ── Build tensor dataset ─────────────────────────────────────
    #
    # Row-aligned tensors: ids / attention / per-head labels / masks.
    # Span-indexed sidetables (rebuilt per-batch below): record the
    # role target + per-token selector mask for every span in every
    # row.  At train time we select spans belonging to the current
    # batch by (span.row_idx in batch) to form the per-span tensors.
    ids_rows: list[list[int]] = []
    offsets_rows: list[list[tuple[int, int]]] = []
    intents: list[int] = []
    topics: list[int] = []
    admissions: list[int] = []
    state_changes: list[int] = []
    shape_intents: list[int] = []
    topic_mask: list[int] = []
    admission_mask: list[int] = []
    state_change_mask: list[int] = []
    shape_intent_mask: list[int] = []
    # Per-row list of (token_mask, role_id) pairs.  Empty list when
    # no role_spans were derived (skipped by role loss).
    row_spans: list[list[tuple[list[float], int]]] = []

    for ex in examples:
        if ex.domain != domain:
            continue
        encoded = tokenizer(
            ex.text,
            padding="max_length",
            truncation=True,
            max_length=manifest.max_length,
            return_offsets_mapping=True,
        )
        input_ids = encoded["input_ids"]
        offsets = [tuple(p) for p in encoded["offset_mapping"]]

        ids_rows.append(input_ids)
        offsets_rows.append(offsets)
        intents.append(intent_idx.get(ex.intent, 0))

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
        if ex.state_change is not None and ex.state_change in state_idx:
            state_changes.append(state_idx[ex.state_change])
            state_change_mask.append(1)
        else:
            state_changes.append(0)
            state_change_mask.append(0)
        shape_val = getattr(ex, "shape_intent", None)
        if shape_val is not None and shape_val in shape_idx:
            shape_intents.append(shape_idx[shape_val])
            shape_intent_mask.append(1)
        else:
            shape_intents.append(0)
            shape_intent_mask.append(0)

        # Role-span targets.  Bootstrap from legacy ``slots`` when
        # absent so v6 corpora train without a separate pre-pass.
        role_spans = ex.role_spans or derive_role_spans_from_slots(
            ex.text, ex.slots, ex.domain,
        )
        per_row: list[tuple[list[float], int]] = []
        for rs in role_spans:
            if rs.role not in role_idx:
                continue
            token_mask = _char_span_to_token_mask(
                offsets, rs.char_start, rs.char_end, manifest.max_length,
            )
            if sum(token_mask) == 0:
                continue  # span fell outside the truncated window
            per_row.append((token_mask, role_idx[rs.role]))
        row_spans.append(per_row)

    if not ids_rows:
        raise RuntimeError(f"no examples for domain {domain!r}")

    ids_t = torch.tensor(ids_rows, dtype=torch.long, device=device)
    mask_t = (ids_t != tokenizer.pad_token_id).long()
    intents_t = torch.tensor(intents, dtype=torch.long, device=device)
    topics_t = torch.tensor(topics, dtype=torch.long, device=device)
    admissions_t = torch.tensor(admissions, dtype=torch.long, device=device)
    state_changes_t = torch.tensor(
        state_changes, dtype=torch.long, device=device,
    )
    shape_intents_t = torch.tensor(
        shape_intents, dtype=torch.long, device=device,
    )
    topic_mask_t = torch.tensor(topic_mask, dtype=torch.bool, device=device)
    admission_mask_t = torch.tensor(
        admission_mask, dtype=torch.bool, device=device,
    )
    state_change_mask_t = torch.tensor(
        state_change_mask, dtype=torch.bool, device=device,
    )
    shape_intent_mask_t = torch.tensor(
        shape_intent_mask, dtype=torch.bool, device=device,
    )

    intent_loss_fn = nn.CrossEntropyLoss()
    role_loss_fn = nn.CrossEntropyLoss()
    head_loss_fn = nn.CrossEntropyLoss(reduction="none")

    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=learning_rate,
    )

    n = ids_t.size(0)
    model.train()
    final_avg_loss = float("nan")
    total_role_spans = sum(len(spans) for spans in row_spans)
    logger.info(
        "[lora] v7 train: %d rows, %d role spans (avg %.1f/row)",
        n, total_role_spans, total_role_spans / max(n, 1),
    )

    for epoch in range(epochs):
        total_loss = 0.0
        perm = torch.randperm(n, device=device)
        for start in range(0, n, batch_size):
            batch_idx_cpu = perm[start:start + batch_size].cpu().tolist()
            batch_idx = torch.tensor(
                batch_idx_cpu, dtype=torch.long, device=device,
            )

            sequence, pooled = model.encode(
                ids_t[batch_idx], mask_t[batch_idx],
            )
            logits = model.classify_pooled(pooled)

            # Pooled-head losses.
            intent_loss = intent_loss_fn(
                logits["intent"], intents_t[batch_idx],
            )
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
            shape_per = head_loss_fn(
                logits["shape_intent"][:, :len(shape_intent_labels) or 1],
                shape_intents_t[batch_idx],
            )
            shape_m = shape_intent_mask_t[batch_idx].float()
            shape_loss = (
                (shape_per * shape_m).sum() / (shape_m.sum() + 1e-9)
                if shape_m.sum() > 0 else torch.tensor(0.0, device=device)
            )

            # Role-head loss — collect this batch's role spans,
            # compute span-pooled vectors, CE over them.
            span_row_idx_list: list[int] = []
            span_masks_list: list[list[float]] = []
            span_role_list: list[int] = []
            for batch_pos, row_idx in enumerate(batch_idx_cpu):
                for token_mask, role_id in row_spans[row_idx]:
                    span_row_idx_list.append(batch_pos)
                    span_masks_list.append(token_mask)
                    span_role_list.append(role_id)

            if span_masks_list:
                span_row_idx_t = torch.tensor(
                    span_row_idx_list, dtype=torch.long, device=device,
                )
                span_masks_t = torch.tensor(
                    span_masks_list, dtype=torch.float32, device=device,
                )  # (S, L)
                span_roles_t = torch.tensor(
                    span_role_list, dtype=torch.long, device=device,
                )
                # Gather the corresponding hidden sequences and pool.
                selected = sequence[span_row_idx_t]     # (S, L, H)
                mask_ = span_masks_t.unsqueeze(-1)      # (S, L, 1)
                pooled_spans = (selected * mask_).sum(dim=1) / \
                    span_masks_t.sum(dim=1, keepdim=True).clamp(min=1e-9)
                role_logits = model.classify_roles(
                    pooled_spans,
                )[:, :len(role_labels) or 1]
                role_loss = role_loss_fn(role_logits, span_roles_t)
            else:
                role_loss = torch.tensor(0.0, device=device)

            loss = (
                intent_loss + role_loss
                + topic_loss + admit_loss + state_loss + shape_loss
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
    manifest.role_labels = role_labels
    manifest.trained_on = {
        "n_examples": n,
        "n_role_spans": total_role_spans,
        "epochs": epochs,
    }
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
                "role_labels": role_labels,
                "topic_labels": topic_labels,
                "admission_labels": admission_labels,
                "state_change_labels": state_change_labels,
                "shape_intent_labels": shape_intent_labels,
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
    """Inference wrapper around a trained LoRA adapter artifact (v7)."""

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

        from peft import PeftModel

        role_labels = self._manifest.role_labels or list(ROLE_LABELS)
        self._model = LoraJointModel(
            encoder_name=self._manifest.encoder,
            n_intents=len(self._manifest.intent_labels),
            n_roles=len(role_labels),
            n_topics=len(self._manifest.topic_labels),
            n_admission=len(self._manifest.admission_labels),
            n_state_change=len(self._manifest.state_change_labels),
            n_shape_intents=len(self._manifest.shape_intent_labels),
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

        # ── 1. Gazetteer pass (catalog-authoritative span detection) ──
        gazetteer_spans = detect_spans(text, domain=self._manifest.domain)

        # ── 2. Encoder forward pass ──────────────────────────────
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
        offsets = [tuple(p) for p in encoded["offset_mapping"][0].tolist()]

        with torch.no_grad():
            sequence, pooled = self._model.encode(input_ids, mask)
            logits = self._model.classify_pooled(pooled)

        # ── 3. Pooled-head decoding ──────────────────────────────
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
        shape_intent, shape_intent_conf = self._argmax_one_hot(
            logits["shape_intent"][0], self._manifest.shape_intent_labels,
        )

        # ── 4. Role head over gazetteer spans ────────────────────
        role_spans_out, slots = self._classify_spans(
            sequence[0], offsets, gazetteer_spans,
        )

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
            shape_intent=shape_intent,  # type: ignore[arg-type]
            shape_intent_confidence=shape_intent_conf,
            role_spans=tuple(role_spans_out),
            method=self.name,
        )

    def _classify_spans(
        self,
        sequence_row: torch.Tensor,          # (L, H)
        offsets: list[tuple[int, int]],
        gazetteer_spans: tuple[DetectedSpan, ...],
    ) -> tuple[list[RoleSpan], dict[str, str]]:
        """Run the role head over every gazetteer span; rebuild slots.

        Returns ``(role_spans, slots)``:
          * ``role_spans`` — one :class:`RoleSpan` per detected
            catalog surface, in detection order (left-to-right).
          * ``slots`` — reconstructed slot dict:
              primary-role spans → ``slots[catalog_slot] = canonical``
              alternative-role spans → ``slots["alternative"] = canonical``
              (first occurrence wins per slot key)
            casual / not_relevant spans are NOT surfaced in ``slots``
            but ARE returned in ``role_spans`` for downstream use.
        """
        role_labels = self._manifest.role_labels or list(ROLE_LABELS)
        if not gazetteer_spans:
            return [], {}

        # Build token masks for each span.
        span_masks: list[list[float]] = []
        for s in gazetteer_spans:
            span_masks.append(_char_span_to_token_mask(
                offsets, s.char_start, s.char_end, self._manifest.max_length,
            ))

        # Drop spans that fell outside the truncated window.
        valid_pairs: list[tuple[DetectedSpan, list[float]]] = [
            (s, m) for s, m in zip(gazetteer_spans, span_masks, strict=True)
            if sum(m) > 0
        ]
        if not valid_pairs:
            return [], {}

        masks_t = torch.tensor(
            [m for _, m in valid_pairs],
            dtype=torch.float32, device=self._device,
        )
        # sequence_row is (L, H); broadcast to per-span pool.
        # (S, L) * (L, H) via outer: use einsum for clarity.
        with torch.no_grad():
            pooled = masks_t @ sequence_row  # (S, L) @ (L, H) = (S, H)
            pooled = pooled / masks_t.sum(dim=1, keepdim=True).clamp(min=1e-9)
            logits = self._model.classify_roles(
                pooled,
            )[:, :len(role_labels) or 1]
            probs = torch.softmax(logits, dim=-1)

        role_spans_out: list[RoleSpan] = []
        # Track best-confidence primary per slot + best-confidence
        # alternative.  Multiple same-slot surfaces with role=primary
        # are common when a row compares two options side by side
        # ("Playwright (by Microsoft) | Selenium (by the Selenium Project)"
        # — both tools legitimately primary).  First-wins discarded
        # the equally-valid later one; max-confidence picks the one
        # the role head was most sure about.
        best_primary: dict[str, tuple[float, str]] = {}
        best_alternative: tuple[float, str] | None = None
        for i, (s, _) in enumerate(valid_pairs):
            idx = int(torch.argmax(probs[i]).item())
            role = role_labels[idx]
            conf = float(probs[i][idx].item())
            rs = RoleSpan(
                char_start=s.char_start,
                char_end=s.char_end,
                surface=s.surface,
                canonical=s.canonical,
                slot=s.slot,
                role=role,  # type: ignore[arg-type]
                source="role-head",
            )
            role_spans_out.append(rs)
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
        return role_spans_out, slots

    def _argmax_one_hot(
        self, head_logits: torch.Tensor, label_vocab: list[str],
    ) -> tuple[str | None, float | None]:
        """Softmax argmax over a single head."""
        if not label_vocab:
            return None, None
        n = len(label_vocab)
        probs = torch.softmax(head_logits[:n], dim=-1)
        idx = int(torch.argmax(probs).item())
        return label_vocab[idx], float(probs[idx].item())


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    domain: Domain,
    encoder: str = "bert-base-uncased",
    topic_labels: list[str] | None = None,
    admission_labels: list[str] | None = None,
    state_change_labels: list[str] | None = None,
    shape_intent_labels: list[str] | None = None,
    role_labels: list[str] | None = None,
    lora_r: int = 8,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    lora_target_modules: list[str] | None = None,
    max_length: int = 128,
    version: str = "v1",
) -> AdapterManifest:
    """Compose a manifest ready for :func:`train`.

    Intent + role vocabs default to the shared schemas.  Topic /
    admission / state-change / shape-intent vocabs come from the
    caller's taxonomy file (YAML) or training-data discovery.  Empty
    lists are allowed — the corresponding head still exists but its
    output is treated as "no label" at inference.
    """
    return AdapterManifest(
        encoder=encoder,
        domain=domain,
        version=version,
        max_length=max_length,
        intent_labels=list(INTENT_CATEGORIES),
        role_labels=role_labels or list(ROLE_LABELS),
        topic_labels=topic_labels or [],
        admission_labels=(
            admission_labels if admission_labels is not None
            else list(ADMISSION_DECISIONS)
        ),
        state_change_labels=(
            state_change_labels if state_change_labels is not None
            else list(STATE_CHANGES)
        ),
        shape_intent_labels=shape_intent_labels or [],
        slot_labels=[],   # v7: BIO vocab retired
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
    "derive_role_spans_from_slots",
    "train",
]
