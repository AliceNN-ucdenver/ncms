"""Tier 3 — LoRA adapter + multi-head Joint BERT (v9 five-head).

Architecture (v9)::

    bert-base-uncased            ← frozen at production
      └── LoRA adapter           ← ~10 MB, per deployment
             ├── intent_head     ← preference: 6 classes ([CLS]-pooled)
             ├── topic_head      ← domain taxonomy ([CLS]-pooled)
             ├── admission_head  ← persist / ephemeral / discard
             ├── state_change    ← declaration / retirement / none
             └── role_head       ← primary / alternative / casual /
                                    not_relevant (per gazetteer span,
                                    span-pooled over subwords)

Catalog-gazetteer split: slot detection is owned by the catalog
(:func:`ncms.application.adapters.sdg.catalog.detect_spans`) — the
authoritative per-domain catalog beats a learned BIO tagger on
coverage.  The SLM's job is the nuance the gazetteer can't see: is
this surface the primary subject of the utterance, an alternative
being rejected, a casual mention, or irrelevant noise?  Final
``slots`` dict is reconstructed from role-labeled spans at
inference time.

History — retired heads:

* **v6 ``slot_head``** (BIO slot tagger) — replaced by the v7 role
  head + catalog gazetteer.
* **v7.x ``shape_intent_head``** (13-class query-shape classifier)
  — overfit template scaffolds; retrospective at
  ``docs/completed/failed-experiments/shape-intent-classification.md``.
* **v8 ``shape_cue_head``** (33-label BIO CTLG cue tagger) — joint
  training saturated; the shared encoder couldn't serve both the
  sequence-labeling cue head and the pooled + span-pooled heads.
  Moved to a dedicated CTLG adapter (future work); retrospective
  at ``docs/completed/failed-experiments/ctlg-joint-cue-head.md``.

Per-example multi-head label masking: rows without a given label
(topic / admission / state_change / role) contribute zero loss for
that head.

Artifact format::

    adapters/<domain>/<version>/
      ├── lora_adapter/        ← peft save_pretrained dir
      ├── heads.safetensors    ← 5 live heads
      ├── manifest.json        ← encoder, label vocabs, train metrics
      ├── taxonomy.yaml        ← human-readable label vocab snapshot
      └── eval_report.md       ← gate metrics at promotion time
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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

    v9 head layout (v6 slot_labels + v7.x shape_intent_labels +
    v8 cue_labels all removed):

      * ``intent_labels``         — 6-class preference intent
      * ``role_labels``           — primary/alternative/casual/
                                     not_relevant (span-pooled)
      * ``topic_labels``          — domain taxonomy
      * ``admission_labels``      — persist/ephemeral/discard
      * ``state_change_labels``   — declaration/retirement/none
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
    """BERT (or BERT+LoRA) + five v9 heads.

    Heads: intent, topic, admission, state_change (four [CLS]-pooled
    heads), role (span-pooled over gazetteer surfaces).

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
        span pooling.
        ``pooled`` is (B, H) — needed by the four [CLS]-pooled heads.
        """
        out = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
        )
        sequence = out.last_hidden_state  # (B, L, H)
        pooled = sequence[:, 0, :]  # [CLS]
        return sequence, pooled

    def classify_pooled(
        self,
        pooled: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        """Apply the 4 [CLS]-pooled heads."""
        return {
            "intent": self.intent_head(pooled),
            "topic": self.topic_head(pooled),
            "admission": self.admission_head(pooled),
            "state_change": self.state_change_head(pooled),
        }

    def classify_roles(
        self,
        span_vectors: torch.Tensor,
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
        """Dump the 5 live v9 heads to a single safetensors file.

        v9 heads: intent, role, topic, admission, state_change.
        Retired (v8 / earlier) heads are NOT in the v9 artifact
        layout.
        """
        state = {
            f"{k}.{sk}": v
            for k, sv in [
                ("intent_head", dict(self.intent_head.state_dict())),
                ("role_head", dict(self.role_head.state_dict())),
                ("topic_head", dict(self.topic_head.state_dict())),
                ("admission_head", dict(self.admission_head.state_dict())),
                ("state_change_head", dict(self.state_change_head.state_dict())),
            ]
            for sk, v in sv.items()
        }
        save_safetensors(state, str(path))

    def load_heads(self, path: Path) -> None:
        """Restore the 5 v9 heads from safetensors.

        Fails loudly (ValueError) on unknown heads / shape mismatches
        rather than the previous silent "skip retired tensor" path —
        v9 is a clean break from v6/v7/v8 artifacts, no hot-swap path.
        Re-train to move a domain onto v9.
        """
        state = load_safetensors(str(path))
        expected = {
            "intent_head",
            "role_head",
            "topic_head",
            "admission_head",
            "state_change_head",
        }
        seen: set[str] = set()
        for key, tensor in state.items():
            head, _, param = key.partition(".")
            seen.add(head)
            if head not in expected:
                raise ValueError(
                    f"[lora] refusing to load checkpoint: unknown head "
                    f"{head!r} (likely a pre-v9 artifact — v9 is a "
                    f"clean break, retrain to migrate).  Known v9 "
                    f"heads: {sorted(expected)}"
                )
            module = getattr(self, head)
            current = module.state_dict().get(param)
            if current is None:
                raise ValueError(f"[lora] checkpoint has unknown param {param!r} on {head!r}")
            if current.shape != tensor.shape:
                raise ValueError(
                    f"[lora] shape mismatch on {head}.{param}: "
                    f"checkpoint={tuple(tensor.shape)} "
                    f"current={tuple(current.shape)}"
                )
            current.copy_(tensor)
        missing = expected - seen
        if missing:
            raise ValueError(f"[lora] checkpoint missing heads: {sorted(missing)}")


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


def _build_lora_model(
    *, manifest: AdapterManifest, role_labels: list[str], device: str
) -> LoraJointModel:
    model = LoraJointModel(
        encoder_name=manifest.encoder,
        n_intents=len(manifest.intent_labels),
        n_roles=len(role_labels),
        n_topics=len(manifest.topic_labels),
        n_admission=len(manifest.admission_labels),
        n_state_change=len(manifest.state_change_labels),
    )
    model.wrap_encoder_with_lora(
        lora_r=manifest.lora_r,
        lora_alpha=manifest.lora_alpha,
        lora_dropout=manifest.lora_dropout,
        lora_target_modules=manifest.lora_target_modules,
    )
    return model.to(device)


def _resolve_label_idx(*, label: str | None, idx_map: dict[str, int]) -> tuple[int, int]:
    """Returns (label_id, mask) — mask=0 when label missing/unknown."""
    if label is not None and label in idx_map:
        return idx_map[label], 1
    return 0, 0


def _build_role_targets_for_row(
    *,
    role_spans: tuple[Any, ...] | list[Any],
    offsets: list[tuple[int, int]],
    role_idx: dict[str, int],
    max_length: int,
) -> list[tuple[list[float], int]]:
    out: list[tuple[list[float], int]] = []
    for rs in role_spans:
        if rs.role not in role_idx:
            continue
        token_mask = _char_span_to_token_mask(
            offsets,
            rs.char_start,
            rs.char_end,
            max_length,
        )
        if sum(token_mask) == 0:
            continue  # span fell outside truncated window
        out.append((token_mask, role_idx[rs.role]))
    return out


@dataclass
class _TrainDataset:
    ids_rows: list[list[int]]
    intents: list[int]
    topics: list[int]
    admissions: list[int]
    state_changes: list[int]
    topic_mask: list[int]
    admission_mask: list[int]
    state_change_mask: list[int]
    row_spans: list[list[tuple[list[float], int]]]


def _build_dataset(
    *,
    examples: list[GoldExample],
    domain: Domain,
    tokenizer: Any,
    manifest: AdapterManifest,
    intent_idx: dict[str, int],
    role_idx: dict[str, int],
    topic_idx: dict[str, int],
    admission_idx: dict[str, int],
    state_idx: dict[str, int],
) -> _TrainDataset:
    ds = _TrainDataset(
        ids_rows=[],
        intents=[],
        topics=[],
        admissions=[],
        state_changes=[],
        topic_mask=[],
        admission_mask=[],
        state_change_mask=[],
        row_spans=[],
    )
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
        ds.ids_rows.append(encoded["input_ids"])
        offsets = [tuple(p) for p in encoded["offset_mapping"]]
        ds.intents.append(intent_idx.get(ex.intent, 0))

        for label, idx_map, label_list, mask_list in (
            (ex.topic, topic_idx, ds.topics, ds.topic_mask),
            (ex.admission, admission_idx, ds.admissions, ds.admission_mask),
            (ex.state_change, state_idx, ds.state_changes, ds.state_change_mask),
        ):
            label_id, mask = _resolve_label_idx(label=label, idx_map=idx_map)
            label_list.append(label_id)
            mask_list.append(mask)

        ds.row_spans.append(
            _build_role_targets_for_row(
                role_spans=ex.role_spans,
                offsets=offsets,
                role_idx=role_idx,
                max_length=manifest.max_length,
            )
        )
    return ds


def _masked_head_loss(
    *,
    logits: torch.Tensor,
    targets: torch.Tensor,
    mask_float: torch.Tensor,
    head_loss_fn: nn.Module,
    n_labels: int,
    device: str,
) -> torch.Tensor:
    per = head_loss_fn(logits[:, : n_labels or 1], targets)
    if mask_float.sum() > 0:
        return (per * mask_float).sum() / (mask_float.sum() + 1e-9)
    return torch.tensor(0.0, device=device)


def _compute_role_loss(
    *,
    sequence: torch.Tensor,
    batch_idx_cpu: list[int],
    row_spans: list[list[tuple[list[float], int]]],
    model: LoraJointModel,
    role_loss_fn: nn.Module,
    n_role_labels: int,
    device: str,
) -> torch.Tensor:
    span_row_idx_list: list[int] = []
    span_masks_list: list[list[float]] = []
    span_role_list: list[int] = []
    for batch_pos, row_idx in enumerate(batch_idx_cpu):
        for token_mask, role_id in row_spans[row_idx]:
            span_row_idx_list.append(batch_pos)
            span_masks_list.append(token_mask)
            span_role_list.append(role_id)
    if not span_masks_list:
        return torch.tensor(0.0, device=device)
    span_row_idx_t = torch.tensor(span_row_idx_list, dtype=torch.long, device=device)
    span_masks_t = torch.tensor(span_masks_list, dtype=torch.float32, device=device)
    span_roles_t = torch.tensor(span_role_list, dtype=torch.long, device=device)
    selected = sequence[span_row_idx_t]  # (S, L, H)
    mask_ = span_masks_t.unsqueeze(-1)  # (S, L, 1)
    pooled_spans = (selected * mask_).sum(dim=1) / span_masks_t.sum(dim=1, keepdim=True).clamp(
        min=1e-9
    )
    role_logits = model.classify_roles(pooled_spans)[:, : n_role_labels or 1]
    return role_loss_fn(role_logits, span_roles_t)


@dataclass
class _BatchTensors:
    ids_t: torch.Tensor
    mask_t: torch.Tensor
    intents_t: torch.Tensor
    topics_t: torch.Tensor
    admissions_t: torch.Tensor
    state_changes_t: torch.Tensor
    topic_mask_t: torch.Tensor
    admission_mask_t: torch.Tensor
    state_change_mask_t: torch.Tensor


def _materialise_tensors(*, ds: _TrainDataset, tokenizer: Any, device: str) -> _BatchTensors:
    ids_t = torch.tensor(ds.ids_rows, dtype=torch.long, device=device)
    return _BatchTensors(
        ids_t=ids_t,
        mask_t=(ids_t != tokenizer.pad_token_id).long(),
        intents_t=torch.tensor(ds.intents, dtype=torch.long, device=device),
        topics_t=torch.tensor(ds.topics, dtype=torch.long, device=device),
        admissions_t=torch.tensor(ds.admissions, dtype=torch.long, device=device),
        state_changes_t=torch.tensor(ds.state_changes, dtype=torch.long, device=device),
        topic_mask_t=torch.tensor(ds.topic_mask, dtype=torch.bool, device=device),
        admission_mask_t=torch.tensor(ds.admission_mask, dtype=torch.bool, device=device),
        state_change_mask_t=torch.tensor(ds.state_change_mask, dtype=torch.bool, device=device),
    )


def _train_one_batch(
    *,
    model: LoraJointModel,
    optimizer: torch.optim.Optimizer,
    tensors: _BatchTensors,
    batch_idx_cpu: list[int],
    batch_idx: torch.Tensor,
    row_spans: list[list[tuple[list[float], int]]],
    manifest: AdapterManifest,
    role_labels: list[str],
    intent_loss_fn: nn.Module,
    role_loss_fn: nn.Module,
    head_loss_fn: nn.Module,
    device: str,
) -> torch.Tensor:
    sequence, pooled = model.encode(tensors.ids_t[batch_idx], tensors.mask_t[batch_idx])
    logits = model.classify_pooled(pooled)
    intent_loss = intent_loss_fn(logits["intent"], tensors.intents_t[batch_idx])
    topic_loss = _masked_head_loss(
        logits=logits["topic"],
        targets=tensors.topics_t[batch_idx],
        mask_float=tensors.topic_mask_t[batch_idx].float(),
        head_loss_fn=head_loss_fn,
        n_labels=len(manifest.topic_labels),
        device=device,
    )
    admit_loss = _masked_head_loss(
        logits=logits["admission"],
        targets=tensors.admissions_t[batch_idx],
        mask_float=tensors.admission_mask_t[batch_idx].float(),
        head_loss_fn=head_loss_fn,
        n_labels=len(manifest.admission_labels),
        device=device,
    )
    state_loss = _masked_head_loss(
        logits=logits["state_change"],
        targets=tensors.state_changes_t[batch_idx],
        mask_float=tensors.state_change_mask_t[batch_idx].float(),
        head_loss_fn=head_loss_fn,
        n_labels=len(manifest.state_change_labels),
        device=device,
    )
    role_loss = _compute_role_loss(
        sequence=sequence,
        batch_idx_cpu=batch_idx_cpu,
        row_spans=row_spans,
        model=model,
        role_loss_fn=role_loss_fn,
        n_role_labels=len(role_labels),
        device=device,
    )
    loss = intent_loss + role_loss + topic_loss + admit_loss + state_loss
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    return loss


def _persist_adapter(
    *,
    model: LoraJointModel,
    manifest: AdapterManifest,
    adapter_dir: Path,
    n: int,
    total_role_spans: int,
    epochs: int,
    final_avg_loss: float,
    role_labels: list[str],
) -> None:
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
    try:
        import yaml

        (adapter_dir / "taxonomy.yaml").write_text(
            yaml.safe_dump(
                {
                    "intent_labels": manifest.intent_labels,
                    "role_labels": role_labels,
                    "topic_labels": manifest.topic_labels,
                    "admission_labels": manifest.admission_labels,
                    "state_change_labels": manifest.state_change_labels,
                },
                sort_keys=False,
            ),
        )
    except ImportError:
        pass
    logger.info("[lora] adapter written to %s", adapter_dir)


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
    """Fine-tune the LoRA adapter + heads on ``examples`` (v9).

    Per-head loss:
      * intent, topic, admission, state_change — cross-entropy,
        skipped per-example when the label is ``None`` (per-head mask).
      * role — cross-entropy over span-pooled representations.  Rows
        with zero explicit ``role_spans`` contribute nothing to the
        role loss (per-row mask).
      * Total loss = sum of non-skipped heads.
    """
    adapter_dir.mkdir(parents=True, exist_ok=True)
    device = device or _pick_device()

    role_labels = manifest.role_labels or list(ROLE_LABELS)
    tokenizer = AutoTokenizer.from_pretrained(manifest.encoder, use_fast=True)
    model = _build_lora_model(manifest=manifest, role_labels=role_labels, device=device)

    intent_idx = {label: i for i, label in enumerate(manifest.intent_labels)}
    role_idx = {label: i for i, label in enumerate(role_labels)}
    topic_idx = {label: i for i, label in enumerate(manifest.topic_labels)}
    admission_idx = {label: i for i, label in enumerate(manifest.admission_labels)}
    state_idx = {label: i for i, label in enumerate(manifest.state_change_labels)}

    ds = _build_dataset(
        examples=examples,
        domain=domain,
        tokenizer=tokenizer,
        manifest=manifest,
        intent_idx=intent_idx,
        role_idx=role_idx,
        topic_idx=topic_idx,
        admission_idx=admission_idx,
        state_idx=state_idx,
    )
    if not ds.ids_rows:
        raise RuntimeError(f"no examples for domain {domain!r}")

    tensors = _materialise_tensors(ds=ds, tokenizer=tokenizer, device=device)

    intent_loss_fn = nn.CrossEntropyLoss()
    role_loss_fn = nn.CrossEntropyLoss()
    head_loss_fn = nn.CrossEntropyLoss(reduction="none")
    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=learning_rate,
    )

    n = tensors.ids_t.size(0)
    model.train()
    final_avg_loss = float("nan")
    total_role_spans = sum(len(spans) for spans in ds.row_spans)
    logger.info(
        "[lora] v9 train: %d rows, %d role spans (avg %.1f/row), lora_r=%d lora_alpha=%d",
        n,
        total_role_spans,
        total_role_spans / max(n, 1),
        manifest.lora_r,
        manifest.lora_alpha,
    )

    for epoch in range(epochs):
        total_loss = 0.0
        perm = torch.randperm(n, device=device)
        for start in range(0, n, batch_size):
            batch_idx_cpu = perm[start : start + batch_size].cpu().tolist()
            batch_idx = torch.tensor(batch_idx_cpu, dtype=torch.long, device=device)
            loss = _train_one_batch(
                model=model,
                optimizer=optimizer,
                tensors=tensors,
                batch_idx_cpu=batch_idx_cpu,
                batch_idx=batch_idx,
                row_spans=ds.row_spans,
                manifest=manifest,
                role_labels=role_labels,
                intent_loss_fn=intent_loss_fn,
                role_loss_fn=role_loss_fn,
                head_loss_fn=head_loss_fn,
                device=device,
            )
            total_loss += float(loss.item()) * batch_idx.size(0)
        final_avg_loss = total_loss / n
        logger.info("[lora] epoch %d/%d loss=%.4f", epoch + 1, epochs, final_avg_loss)

    _persist_adapter(
        model=model,
        manifest=manifest,
        adapter_dir=adapter_dir,
        n=n,
        total_role_spans=total_role_spans,
        epochs=epochs,
        final_avg_loss=final_avg_loss,
        role_labels=role_labels,
    )
    return manifest


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class LoraJointBert(IntentSlotExtractor):
    """Inference wrapper around a trained LoRA adapter artifact (v9)."""

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
            self._manifest.encoder,
            use_fast=True,
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
        )
        self._model.encoder = PeftModel.from_pretrained(
            self._model.encoder,
            str(adapter_dir / "lora_adapter"),
        )
        self._model.load_heads(adapter_dir / "heads.safetensors")
        self._model.to(self._device).eval()

    def extract(
        self,
        text: str,
        *,
        domain: Domain,
    ) -> ExtractedLabel:
        if domain != self._manifest.domain:
            logger.debug(
                "[lora] cross-domain call: adapter=%s request=%s",
                self._manifest.domain,
                domain,
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
            logits["intent"][0],
            self._manifest.intent_labels,
        )
        topic, topic_conf = self._argmax_one_hot(
            logits["topic"][0],
            self._manifest.topic_labels,
        )
        admission, admission_conf = self._argmax_one_hot(
            logits["admission"][0],
            self._manifest.admission_labels,
        )
        state_change, state_change_conf = self._argmax_one_hot(
            logits["state_change"][0],
            self._manifest.state_change_labels,
        )

        # ── 4. Role head over gazetteer spans ────────────────────
        role_spans_out, slots = self._classify_spans(
            sequence[0],
            offsets,
            gazetteer_spans,
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
            role_spans=tuple(role_spans_out),
            method=self.name,
        )

    def _classify_spans(
        self,
        sequence_row: torch.Tensor,  # (L, H)
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
            span_masks.append(
                _char_span_to_token_mask(
                    offsets,
                    s.char_start,
                    s.char_end,
                    self._manifest.max_length,
                )
            )

        # Drop spans that fell outside the truncated window.
        valid_pairs: list[tuple[DetectedSpan, list[float]]] = [
            (s, m) for s, m in zip(gazetteer_spans, span_masks, strict=True) if sum(m) > 0
        ]
        if not valid_pairs:
            return [], {}

        masks_t = torch.tensor(
            [m for _, m in valid_pairs],
            dtype=torch.float32,
            device=self._device,
        )
        # sequence_row is (L, H); broadcast to per-span pool.
        # (S, L) * (L, H) via outer: use einsum for clarity.
        with torch.no_grad():
            pooled = masks_t @ sequence_row  # (S, L) @ (L, H) = (S, H)
            pooled = pooled / masks_t.sum(dim=1, keepdim=True).clamp(min=1e-9)
            logits = self._model.classify_roles(
                pooled,
            )[:, : len(role_labels) or 1]
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

        slots: dict[str, str] = {slot: canon for slot, (_, canon) in best_primary.items()}
        if best_alternative is not None:
            slots["alternative"] = best_alternative[1]
        return role_spans_out, slots

    def _argmax_one_hot(
        self,
        head_logits: torch.Tensor,
        label_vocab: list[str],
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
    lora_r: int = 16,
    lora_alpha: int = 32,
    lora_dropout: float = 0.05,
    lora_target_modules: list[str] | None = None,
    max_length: int = 128,
    version: str = "v1",
) -> AdapterManifest:
    """Compose a manifest ready for :func:`train`.

    v9 defaults: LoRA rank 16 / alpha 32 (2:1 alpha:r ratio).  Intent
    + role vocabs default to the shared schemas; topic / admission /
    state_change come from the caller's taxonomy file (YAML) or
    training-data discovery.  Empty vocabs are allowed — the
    corresponding head still exists but its output is treated as
    "no label" at inference.
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
            admission_labels if admission_labels is not None else list(ADMISSION_DECISIONS)
        ),
        state_change_labels=(
            state_change_labels if state_change_labels is not None else list(STATE_CHANGES)
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
