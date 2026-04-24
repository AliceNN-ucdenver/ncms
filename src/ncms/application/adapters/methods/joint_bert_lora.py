"""Tier 3 — LoRA adapter + multi-head Joint BERT (v8.1 six-head).

Architecture (v8.1+)::

    bert-base-uncased            ← frozen at production
      └── LoRA adapter           ← 10-15 MB, per deployment
             ├── intent_head     ← preference: 6 classes (pooled [CLS])
             ├── topic_head      ← domain taxonomy (pooled [CLS])
             ├── admission_head  ← persist / ephemeral / discard
             ├── state_change    ← declaration / retirement / none
             ├── role_head       ← primary / alternative / casual /
             │                     not_relevant (per gazetteer span,
             │                     span-pooled over subwords)
             └── shape_cue_head  ← v8+ CTLG per-token BIO tagger over
                                    the 33 causal / temporal / ordinal /
                                    modal / referent / subject / scope
                                    cues; output feeds the compositional
                                    synthesizer (ncms.domain.tlg.
                                    semantic_parser) which composes a
                                    TLGQuery for the dispatcher.

Catalog-gazetteer split: slot detection is owned by the catalog
(:func:`ncms.application.adapters.sdg.catalog.detect_spans`) — the
authoritative software_dev catalog beats a learned BIO tagger on
coverage.  The SLM's job is the nuance the gazetteer can't see: is
this surface the primary subject of the utterance, an alternative
being rejected, a casual mention, or irrelevant noise?  Final
``slots`` dict is reconstructed from role-labeled spans at
inference time.

v8.1 removed the v6 ``slot_head`` (retired BIO tagger, replaced by
role head in v7) and the v7.x ``shape_intent_head`` (13-class
query-shape classifier that overfit template scaffolds — see
``docs/completed/failed-experiments/shape-intent-classification.md``).

Per-example multi-head label masking: rows without a given
label (topic / admission / state_change / role / cue) contribute
zero loss for that head.

Artifact format::

    adapters/<domain>/<version>/
      ├── lora_adapter/        ← peft save_pretrained dir
      ├── heads.safetensors    ← 6 live heads
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

    v8.1 head layout (v6 slot_labels + v7.x shape_intent_labels removed):
      * ``intent_labels``         — 6-class preference intent
      * ``role_labels``           — primary/alternative/casual/
                                     not_relevant (span-pooled)
      * ``topic_labels``          — domain taxonomy
      * ``admission_labels``      — persist/ephemeral/discard
      * ``state_change_labels``   — declaration/retirement/none
      * ``cue_labels``            — CTLG BIO cue vocab (33 tags)
    """

    encoder: str = "bert-base-uncased"
    domain: Domain = "conversational"
    version: str = "v1"
    max_length: int = 128

    intent_labels: list[str] = field(default_factory=list)
    # v7: role labels (4 categories) — span-pooled role head.
    role_labels: list[str] = field(default_factory=list)
    topic_labels: list[str] = field(default_factory=list)
    admission_labels: list[str] = field(default_factory=list)
    state_change_labels: list[str] = field(default_factory=list)
    # v8+ CTLG: BIO cue-label vocabulary for the sequence-labeled
    # 6th head (shape_cue_head).  Should always be the full 33-entry
    # list from ncms.domain.tlg.cue_taxonomy.CUE_LABELS on new trains.
    cue_labels: list[str] = field(default_factory=list)

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
        """Load a manifest, silently dropping unknown keys.

        Forward-compat: a checkpoint trained with a newer manifest
        schema (e.g. a future 7th-head field) still loads cleanly
        into an older runtime; the new field is just ignored.
        """
        raw = json.loads(path.read_text())
        allowed = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in raw.items() if k in allowed})


# ---------------------------------------------------------------------------
# Multi-head model
# ---------------------------------------------------------------------------


class LoraJointModel(nn.Module):
    """BERT (or BERT+LoRA) + six v8.1 heads.

    Heads: intent, topic, admission, state_change (four [CLS]-pooled
    heads), role (span-pooled over gazetteer surfaces), shape_cue
    (per-token BIO sequence labeler).

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
        n_cue_labels: int = 0,
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
        # v8+ CTLG shape_cue_head — per-token BIO cue classifier
        # (Linear over the full sequence output, one logit vector
        # per encoder position).  n_cue_labels=0 on legacy adapters;
        # on v8+ it's len(cue_taxonomy.CUE_LABELS) = 33.
        self.shape_cue_head = nn.Linear(hidden, max(n_cue_labels, 1))

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
        span pooling and the cue head for per-token classification.
        ``pooled`` is (B, H) — needed by the four [CLS]-pooled heads.
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
        """Apply the 4 [CLS]-pooled heads."""
        return {
            "intent": self.intent_head(pooled),
            "topic": self.topic_head(pooled),
            "admission": self.admission_head(pooled),
            "state_change": self.state_change_head(pooled),
        }

    def classify_roles(
        self, span_vectors: torch.Tensor,
    ) -> torch.Tensor:
        """Apply the role head to span-pooled vectors.  Input (S, H);
        output (S, n_roles)."""
        return self.role_head(span_vectors)

    def classify_cues(
        self, sequence: torch.Tensor,
    ) -> torch.Tensor:
        """v8+ CTLG: per-token BIO cue-label classification.

        Input: ``sequence`` of shape (B, L, H) — the full encoder
        output.  Output: (B, L, n_cue_labels) logits.
        """
        return self.shape_cue_head(sequence)

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
        """Dump the 6 live v8.1 heads to a single safetensors file.

        v8.1 heads: intent, role, topic, admission, state_change,
        shape_cue.  Retired in v8.1: slot_head (v6 BIO),
        shape_intent_head (v7.x classifier).
        """
        state = {f"{k}.{sk}": v for k, sv in [
            ("intent_head", dict(self.intent_head.state_dict())),
            ("role_head", dict(self.role_head.state_dict())),
            ("topic_head", dict(self.topic_head.state_dict())),
            ("admission_head", dict(self.admission_head.state_dict())),
            ("state_change_head", dict(self.state_change_head.state_dict())),
            ("shape_cue_head", dict(self.shape_cue_head.state_dict())),
        ] for sk, v in sv.items()}
        save_safetensors(state, str(path))

    def load_heads(self, path: Path) -> None:
        """Restore the heads from safetensors.

        Tolerates unknown keys and shape mismatches so pre-v8.1
        checkpoints (which carry retired ``slot_head`` and/or
        ``shape_intent_head`` tensors) still load cleanly — the
        retired tensors are logged as "unknown head" and skipped.
        Useful for hot-swapping v7.x / v8 adapters onto a v8.1+
        runtime without re-training.
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
    cue_loss_weight: float = 3.0,
) -> AdapterManifest:
    """Fine-tune the LoRA adapter + heads on ``examples`` (v8.1).

    Per-head loss:
      * intent, topic, admission, state_change — cross-entropy,
        skipped per-example when the label is ``None`` (per-head mask).
      * role — cross-entropy over span-pooled representations.  Rows
        with zero explicit ``role_spans`` contribute nothing to the
        role loss (per-row mask).
      * cue — per-token cross-entropy with ignore_index=-100 so
        pad positions are excluded; weighted by ``cue_loss_weight``.
      * Total loss = sum of non-skipped heads.
    """
    adapter_dir.mkdir(parents=True, exist_ok=True)
    device = device or _pick_device()

    role_labels = manifest.role_labels or list(ROLE_LABELS)
    intent_labels = manifest.intent_labels
    topic_labels = manifest.topic_labels
    admission_labels = manifest.admission_labels
    state_change_labels = manifest.state_change_labels
    cue_labels = manifest.cue_labels  # empty → head is a placeholder; no loss

    tokenizer = AutoTokenizer.from_pretrained(manifest.encoder, use_fast=True)
    model = LoraJointModel(
        encoder_name=manifest.encoder,
        n_intents=len(intent_labels),
        n_roles=len(role_labels),
        n_topics=len(topic_labels),
        n_admission=len(admission_labels),
        n_state_change=len(state_change_labels),
        n_cue_labels=len(cue_labels),
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
    cue_idx = {label: i for i, label in enumerate(cue_labels)}

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
    topic_mask: list[int] = []
    admission_mask: list[int] = []
    state_change_mask: list[int] = []
    # Per-row list of (token_mask, role_id) pairs.  Empty list when
    # no role_spans were derived (skipped by role loss).
    row_spans: list[list[tuple[list[float], int]]] = []
    # v8+ CTLG: per-token BIO cue labels.  One int per token in the
    # encoded sequence; -100 on pad / out-of-text positions so
    # CrossEntropyLoss ignores them.  cue_row_has_labels[i]=False
    # means the example didn't carry cue_tags (e.g. legacy v7.x
    # corpus); the row contributes zero to the cue loss.
    cue_row_labels: list[list[int]] = []
    cue_row_has_labels: list[bool] = []

    cue_o_id = cue_idx.get("O", 0) if cue_labels else 0

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

        # Role-span targets.  Rows without explicit role_spans
        # (legacy v6 corpora) contribute nothing to the role loss
        # via the per-row mask — the bootstrap-from-slots helper was
        # removed in v8.1.  Use ``scripts/v7_rollout/relabel_roles.py``
        # to add explicit role_spans to pre-v7 gold before training.
        role_spans = ex.role_spans
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

        # v8+ CTLG: per-token BIO cue labels.  Per-token because the
        # cue head is a sequence labeler (Linear over full encoder
        # output, one logit vector per position).  Labels produce: O
        # for non-cue tokens, B-<TYPE>/I-<TYPE> for tokens inside a
        # tagged span.  -100 on pad / special-token
        # positions so CrossEntropyLoss ignores them.
        row_cues = [-100] * manifest.max_length
        ex_cue_tags = getattr(ex, "cue_tags", None) or []
        if cue_labels:
            # Initialize all non-pad content positions to "O".
            for i, (ts, te) in enumerate(offsets):
                if i >= manifest.max_length:
                    break
                if ts == 0 and te == 0:
                    continue  # padding / special token
                row_cues[i] = cue_o_id
            # Overlay cue-tagged spans.
            for tag in ex_cue_tags:
                # Tag may be a dict (from JSONL loader) or a
                # TaggedToken dataclass; handle both.
                if isinstance(tag, dict):
                    char_start = int(tag["char_start"])
                    char_end = int(tag["char_end"])
                    label_str = tag["cue_label"]
                else:
                    char_start = int(tag.char_start)
                    char_end = int(tag.char_end)
                    label_str = tag.cue_label
                if label_str == "O" or label_str not in cue_idx:
                    continue
                label_id = cue_idx[label_str]
                for i, (ts, te) in enumerate(offsets):
                    if i >= manifest.max_length:
                        break
                    if ts == 0 and te == 0:
                        continue
                    if te <= char_start or ts >= char_end:
                        continue
                    row_cues[i] = label_id
        cue_row_labels.append(row_cues)
        cue_row_has_labels.append(bool(ex_cue_tags) and bool(cue_labels))

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
    topic_mask_t = torch.tensor(topic_mask, dtype=torch.bool, device=device)
    admission_mask_t = torch.tensor(
        admission_mask, dtype=torch.bool, device=device,
    )
    state_change_mask_t = torch.tensor(
        state_change_mask, dtype=torch.bool, device=device,
    )
    cue_labels_t = (
        torch.tensor(cue_row_labels, dtype=torch.long, device=device)
        if cue_labels else None
    )
    cue_row_mask_t = torch.tensor(
        cue_row_has_labels, dtype=torch.bool, device=device,
    ) if cue_labels else None

    intent_loss_fn = nn.CrossEntropyLoss()
    role_loss_fn = nn.CrossEntropyLoss()
    head_loss_fn = nn.CrossEntropyLoss(reduction="none")
    cue_loss_fn = nn.CrossEntropyLoss(ignore_index=-100) if cue_labels else None

    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=learning_rate,
    )

    n = ids_t.size(0)
    model.train()
    final_avg_loss = float("nan")
    total_role_spans = sum(len(spans) for spans in row_spans)
    n_cue_rows = sum(cue_row_has_labels)
    logger.info(
        "[lora] v8.1 train: %d rows, %d role spans (avg %.1f/row), "
        "%d rows with cue_tags (CTLG head %s, cue_loss_weight=%.1f, "
        "lora_r=%d lora_alpha=%d)",
        n, total_role_spans, total_role_spans / max(n, 1),
        n_cue_rows,
        "ACTIVE" if cue_labels else "INACTIVE (no cue_labels in manifest)",
        cue_loss_weight,
        manifest.lora_r, manifest.lora_alpha,
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

            # v8+ CTLG: per-token cue-head loss.  Applies to rows
            # whose cue_row_has_labels was True; other rows drop
            # out via the row mask.  Uses ignore_index=-100 to
            # skip pad positions.
            if cue_labels and cue_loss_fn is not None:
                cue_logits = model.classify_cues(sequence)  # (B, L, n_cue)
                cue_logits = cue_logits[:, :, :len(cue_labels)]
                batch_cue_labels = cue_labels_t[batch_idx]  # type: ignore[index]
                batch_cue_mask = cue_row_mask_t[batch_idx]  # type: ignore[index]
                # Mask entire rows that don't carry cue labels by
                # replacing their per-token labels with -100.
                batch_cue_labels = batch_cue_labels.clone()
                batch_cue_labels[~batch_cue_mask] = -100
                cue_loss = cue_loss_fn(
                    cue_logits.reshape(-1, cue_logits.size(-1)),
                    batch_cue_labels.reshape(-1),
                )
                # cue_loss may be nan when no valid position exists;
                # guard by replacing with zero so the graph doesn't
                # backprop garbage.
                if torch.isnan(cue_loss).any():
                    cue_loss = torch.tensor(0.0, device=device)
            else:
                cue_loss = torch.tensor(0.0, device=device)

            # v8.1: cue head weighted 3× by default — the 33-label
            # BIO sequence task is harder than the [CLS]-pooled heads
            # and was underfit at equal weight.  See docs for the
            # v8 held-out macro F1 = 0.276 that triggered this change.
            loss = (
                intent_loss + role_loss
                + topic_loss + admit_loss + state_loss
                + cue_loss_weight * cue_loss
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
    manifest.cue_labels = cue_labels
    n_cue_rows = sum(cue_row_has_labels)
    manifest.trained_on = {
        "n_examples": n,
        "n_role_spans": total_role_spans,
        "n_cue_rows": n_cue_rows,
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
                "cue_labels": cue_labels,
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
    """Inference wrapper around a trained LoRA adapter artifact (v8.1+)."""

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
            n_cue_labels=len(self._manifest.cue_labels),
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

        # ── 4. Role head over gazetteer spans ────────────────────
        role_spans_out, slots = self._classify_spans(
            sequence[0], offsets, gazetteer_spans,
        )

        # ── 5. CTLG cue head over the full token sequence ────────
        cue_tags_out = self._classify_cues(text, offsets, sequence[0])

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
            role_spans=tuple(role_spans_out),
            cue_tags=tuple(cue_tags_out),
            method=self.name,
        )

    def _classify_cues(
        self,
        text: str,
        offsets: list[tuple[int, int]],
        sequence_row: torch.Tensor,  # (L, H)
    ) -> list:
        """v8+ CTLG: per-token BIO cue classification.

        Returns a list of :class:`TaggedToken` (from
        :mod:`ncms.domain.tlg.cue_taxonomy`) aggregated to
        surface-word granularity.  Empty list when the adapter's
        manifest carries no ``cue_labels`` (legacy v7.x).
        """
        cue_labels = self._manifest.cue_labels
        if not cue_labels:
            return []
        # Import here to avoid pulling the domain layer into module
        # import costs for the training-only code paths.
        from ncms.domain.tlg.cue_taxonomy import TaggedToken

        with torch.no_grad():
            logits = self._model.classify_cues(
                sequence_row.unsqueeze(0),
            )[0, :, :len(cue_labels)]  # (L, n_cue)
            probs = torch.softmax(logits, dim=-1)
            pred_ids = torch.argmax(probs, dim=-1)  # (L,)
            pred_conf = probs.gather(
                -1, pred_ids.unsqueeze(-1),
            ).squeeze(-1)  # (L,)

        # Aggregate BERT wordpieces to surface words.  We walk the
        # offsets and merge consecutive subwords into one
        # TaggedToken whose label is the FIRST subword's label
        # (standard BIO convention for BERT-tokenizer outputs).
        out: list[TaggedToken] = []
        i = 0
        n = len(offsets)
        while i < n:
            ts, te = offsets[i]
            if ts == 0 and te == 0:
                # Special token (CLS/SEP/pad) — skip.
                i += 1
                continue
            # Extend to include subword continuations
            # (subword = next token's start == current's end AND
            # source text has no whitespace gap).
            word_start = ts
            word_end = te
            j = i + 1
            while j < n:
                ns, ne = offsets[j]
                if ns == 0 and ne == 0:
                    break
                if ns != word_end:
                    break  # whitespace break → separate word
                word_end = ne
                j += 1
            # Label = first subword's.
            label_id = int(pred_ids[i].item())
            label = cue_labels[label_id] if label_id < len(cue_labels) else "O"
            conf = float(pred_conf[i].item())
            surface = text[word_start:word_end]
            if label != "O":
                out.append(TaggedToken(
                    char_start=word_start,
                    char_end=word_end,
                    surface=surface,
                    cue_label=label,  # type: ignore[arg-type]
                    confidence=conf,
                ))
            i = j
        return out

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
    role_labels: list[str] | None = None,
    cue_labels: list[str] | None = None,
    lora_r: int = 32,
    lora_alpha: int = 64,
    lora_dropout: float = 0.05,
    lora_target_modules: list[str] | None = None,
    max_length: int = 128,
    version: str = "v1",
) -> AdapterManifest:
    """Compose a manifest ready for :func:`train`.

    v8.1 defaults: LoRA rank 32 / alpha 64 (the 2:1 alpha:r ratio
    PEFT literature recommends), tuned up from the v8 rank 8
    because the cue head underfit at low rank on the 33-label
    sequence task.  Intent + role + cue vocabs default to the
    shared schemas; topic / admission / state_change come from the
    caller's taxonomy file (YAML) or training-data discovery.
    Empty vocabs are allowed — the corresponding head still
    exists but its output is treated as "no label" at inference.

    ``cue_labels`` defaults to the full 33-entry CTLG cue taxonomy
    (see :mod:`ncms.domain.tlg.cue_taxonomy`).  Pass an empty list
    to disable the cue head (e.g. for a pure-preference ablation).
    """
    if cue_labels is None:
        # Default to the full CTLG vocab.  Import late to avoid
        # pulling the domain layer into the training module's
        # import footprint during smoke tests.
        from ncms.domain.tlg.cue_taxonomy import CUE_LABELS as _CUE_LABELS
        cue_labels = list(_CUE_LABELS)

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
        cue_labels=cue_labels,
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
