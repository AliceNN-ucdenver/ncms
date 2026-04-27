"""Dedicated CTLG cue-tagger adapter.

Phase 2 skeleton: manifest, artifact validation, and an inference wrapper
contract.  Training code lands later; this module defines the on-disk shape
and the runtime boundary without touching the v9 five-head SLM.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from ncms.application.adapters.ctlg.corpus import CTLGExample
from ncms.application.adapters.ctlg.token_alignment import expand_bio_to_wordpieces
from ncms.application.adapters.schemas import Domain
from ncms.domain.tlg.cue_taxonomy import CUE_LABELS, CueLabel, TaggedToken

logger = logging.getLogger(__name__)
_SURFACE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._/#:+-][A-Za-z0-9]+)*|[^\w\s]")


class CTLGAdapterIntegrityError(ValueError):
    """Raised when a CTLG adapter artifact fails structural validation."""


@dataclass
class CTLGAdapterManifest:
    """Persisted alongside every dedicated CTLG cue-tagger artifact."""

    encoder: str = "bert-base-uncased"
    domain: Domain = "conversational"
    version: str = "ctlg-v1"
    max_length: int = 128
    cue_labels: list[str] = field(default_factory=lambda: list(CUE_LABELS))

    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    lora_target_modules: list[str] = field(default_factory=lambda: ["query", "value"])

    trained_on: dict[str, int] = field(default_factory=dict)
    gate_metrics: dict[str, float] = field(default_factory=dict)
    trained_at: str = ""
    corpus_hash: str = ""

    def validate(self) -> None:
        """Validate manifest fields that affect runtime decoding."""
        if not self.cue_labels:
            raise CTLGAdapterIntegrityError("manifest cue_labels is empty")
        unknown = sorted(set(self.cue_labels) - set(CUE_LABELS))
        if unknown:
            raise CTLGAdapterIntegrityError(
                f"manifest cue_labels contains unknown labels: {unknown}",
            )
        if "O" not in self.cue_labels:
            raise CTLGAdapterIntegrityError("manifest cue_labels must contain 'O'")
        if self.max_length <= 0:
            raise CTLGAdapterIntegrityError("manifest max_length must be positive")

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> CTLGAdapterManifest:
        raw = json.loads(path.read_text())
        allowed = set(cls.__dataclass_fields__.keys())
        manifest = cls(**{k: v for k, v in raw.items() if k in allowed})
        manifest.validate()
        return manifest


@dataclass
class _CTLGTrainDataset:
    input_ids: list[list[int]]
    attention_masks: list[list[int]]
    labels: list[list[int]]
    label_masks: list[list[bool]]
    n_wordpieces: int


def _label_index(manifest: CTLGAdapterManifest) -> dict[str, int]:
    return {label: idx for idx, label in enumerate(manifest.cue_labels)}


def _word_ids_from_encoded(encoded: object) -> list[int | None]:
    word_ids = getattr(encoded, "word_ids", None)
    if word_ids is None:
        raise RuntimeError("tokenizer output must expose word_ids() for CTLG training")
    return list(word_ids())


def _build_training_dataset(
    *,
    examples: list[CTLGExample],
    domain: Domain,
    tokenizer: Any,
    manifest: CTLGAdapterManifest,
) -> _CTLGTrainDataset:
    """Tokenize CTLG examples and align BIO labels to wordpieces."""
    idx = _label_index(manifest)
    ds = _CTLGTrainDataset(
        input_ids=[],
        attention_masks=[],
        labels=[],
        label_masks=[],
        n_wordpieces=0,
    )
    for ex in examples:
        if ex.domain != domain:
            continue
        encoded = tokenizer(
            list(ex.tokens),
            is_split_into_words=True,
            padding="max_length",
            truncation=True,
            max_length=manifest.max_length,
        )
        wordpiece_labels = expand_bio_to_wordpieces(
            word_labels=ex.cue_tags,
            word_ids=_word_ids_from_encoded(encoded),
        )
        input_ids = list(encoded["input_ids"])
        attention_mask = list(encoded.get("attention_mask", [1] * len(input_ids)))
        labels = [
            idx[label] if keep else -100
            for label, keep in zip(
                wordpiece_labels.labels,
                wordpiece_labels.label_mask,
                strict=True,
            )
        ]
        ds.input_ids.append(input_ids)
        ds.attention_masks.append(attention_mask)
        ds.labels.append(labels)
        ds.label_masks.append(list(wordpiece_labels.label_mask))
        ds.n_wordpieces += sum(wordpiece_labels.label_mask)
    return ds


def _token_accuracy(*, gold: list[str], pred: list[str]) -> float:
    total = len(gold)
    if total == 0:
        return 0.0
    correct = sum(1 for g, p in zip(gold, pred, strict=True) if g == p)
    return correct / total


def _label_f1(*, gold: list[str], pred: list[str], label: str) -> float | None:
    tp = sum(1 for g, p in zip(gold, pred, strict=True) if g == label and p == label)
    fp = sum(1 for g, p in zip(gold, pred, strict=True) if g != label and p == label)
    fn = sum(1 for g, p in zip(gold, pred, strict=True) if g == label and p != label)
    if tp + fp + fn == 0:
        return None
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _family(label: str) -> str:
    if label == "O":
        return "O"
    return label.split("-", 1)[1]


def _non_o_macro_f1(*, gold: list[str], pred: list[str], labels: list[str]) -> float:
    f1s = [_label_f1(gold=gold, pred=pred, label=label) for label in labels if label != "O"]
    supported = [f1 for f1 in f1s if f1 is not None]
    return sum(supported) / len(supported) if supported else 0.0


def _family_metrics(*, gold: list[str], pred: list[str], labels: list[str]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    families = sorted({_family(label) for label in labels if label != "O"})
    for family in families:
        gold_family = [family if _family(label) == family else "O" for label in gold]
        pred_family = [family if _family(label) == family else "O" for label in pred]
        f1 = _label_f1(gold=gold_family, pred=pred_family, label=family)
        if f1 is not None:
            metrics[f"family_{family.lower()}_f1"] = f1
    return metrics


def _label_metrics(*, gold: list[str], pred: list[str], labels: list[str]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for label in labels:
        if label == "O":
            continue
        f1 = _label_f1(gold=gold, pred=pred, label=label)
        if f1 is not None:
            metrics[f"label_{label.lower().replace('-', '_')}_f1"] = f1
    return metrics


def compute_cue_metrics(
    *,
    gold: list[str],
    pred: list[str],
    cue_labels: list[str] | None = None,
) -> dict[str, float]:
    """Compute token-level CTLG cue metrics."""
    if len(gold) != len(pred):
        raise ValueError("gold and pred must have equal length")
    labels = list(cue_labels or CUE_LABELS)
    accuracy = _token_accuracy(gold=gold, pred=pred)
    metrics: dict[str, float] = {
        "token_micro_f1": accuracy,
        "token_accuracy": accuracy,
    }
    metrics["non_o_macro_f1"] = _non_o_macro_f1(gold=gold, pred=pred, labels=labels)
    metrics.update(_family_metrics(gold=gold, pred=pred, labels=labels))
    metrics.update(_label_metrics(gold=gold, pred=pred, labels=labels))
    return metrics


def evaluate_cue_tagger(
    cue_tagger: object,
    examples: list[CTLGExample],
    *,
    domain: Domain,
    cue_labels: list[str] | None = None,
) -> dict[str, float]:
    """Evaluate a CTLG cue tagger against word-level gold examples.

    Runtime taggers may decode tokenizer wordpieces or normalized surface
    pieces, so predictions are projected back onto each gold token by maximum
    character-span overlap.
    """
    extract = getattr(cue_tagger, "extract_cues", None)
    if extract is None:
        raise TypeError("cue_tagger must expose extract_cues(text, domain=...)")

    gold_labels: list[str] = []
    pred_labels: list[str] = []
    n_examples = 0
    for ex in examples:
        if ex.domain != domain:
            continue
        n_examples += 1
        predictions = list(extract(ex.text, domain=domain))
        for offset, gold in zip(ex.char_offsets, ex.cue_tags, strict=True):
            gold_labels.append(gold)
            pred_labels.append(_project_prediction_for_offset(offset, predictions))

    metrics = compute_cue_metrics(
        gold=gold_labels,
        pred=pred_labels,
        cue_labels=cue_labels,
    )
    metrics["n_examples"] = float(n_examples)
    metrics["n_tokens"] = float(len(gold_labels))
    return metrics


def _project_prediction_for_offset(
    offset: tuple[int, int],
    predictions: list[TaggedToken],
) -> str:
    start, end = offset
    best: TaggedToken | None = None
    best_overlap = 0
    for pred in predictions:
        overlap = max(0, min(end, pred.char_end) - max(start, pred.char_start))
        if overlap > best_overlap:
            best = pred
            best_overlap = overlap
    return best.cue_label if best is not None else "O"


def _corpus_hash(examples: list[CTLGExample]) -> str:
    h = hashlib.sha256()
    for ex in examples:
        row = json.dumps(
            {
                "text": ex.text,
                "tokens": ex.tokens,
                "cue_tags": ex.cue_tags,
                "domain": ex.domain,
                "voice": ex.voice,
                "split": ex.split,
            },
            sort_keys=True,
        ).encode("utf-8")
        h.update(row)
    return h.hexdigest()[:16]


def load_ctlg_manifest(adapter_dir: Path) -> CTLGAdapterManifest:
    """Load and validate ``adapter_dir/manifest.json``."""
    manifest_path = adapter_dir / "manifest.json"
    if not manifest_path.is_file():
        raise CTLGAdapterIntegrityError(f"no manifest.json at {manifest_path}")
    try:
        return CTLGAdapterManifest.load(manifest_path)
    except json.JSONDecodeError as exc:
        raise CTLGAdapterIntegrityError(
            f"malformed manifest.json at {manifest_path}: {exc}",
        ) from exc


def verify_ctlg_adapter_dir(adapter_dir: Path) -> CTLGAdapterManifest:
    """Validate the dedicated CTLG adapter artifact layout."""
    if not adapter_dir.is_dir():
        raise CTLGAdapterIntegrityError(f"ctlg adapter_dir does not exist: {adapter_dir}")

    manifest = load_ctlg_manifest(adapter_dir)

    heads_path = adapter_dir / "heads.safetensors"
    if not heads_path.is_file() or heads_path.stat().st_size == 0:
        raise CTLGAdapterIntegrityError(f"missing or empty heads.safetensors at {heads_path}")

    lora_dir = adapter_dir / "lora_adapter"
    if not lora_dir.is_dir():
        raise CTLGAdapterIntegrityError(f"missing lora_adapter/ directory at {lora_dir}")
    if not (lora_dir / "adapter_config.json").is_file():
        raise CTLGAdapterIntegrityError(
            f"missing lora_adapter/adapter_config.json at {lora_dir}",
        )

    return manifest


def _pick_device() -> str:
    try:
        from ncms.infrastructure.hardware import resolve_device

        return resolve_device("NCMS_CTLG_DEVICE")
    except ImportError:
        pass

    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def _materialize_tensors(ds: _CTLGTrainDataset, *, device: str):
    import torch

    return (
        torch.tensor(ds.input_ids, dtype=torch.long, device=device),
        torch.tensor(ds.attention_masks, dtype=torch.long, device=device),
        torch.tensor(ds.labels, dtype=torch.long, device=device),
    )


def _class_weights(labels: Any, *, num_labels: int, device: str):
    import torch

    flat = labels.view(-1)
    kept = flat[flat >= 0]
    if kept.numel() == 0:
        return torch.ones(num_labels, dtype=torch.float32, device=device)
    counts = torch.bincount(kept, minlength=num_labels).float()
    present = counts > 0
    n_present = int(present.sum().item()) or 1
    total = counts[present].sum()
    weights = torch.ones(num_labels, dtype=torch.float32, device=device)
    weights[present] = total / (n_present * counts[present])
    return torch.clamp(weights, min=0.25, max=5.0)


def _example_sampling_weights(labels: Any, *, num_labels: int, device: str):
    import torch

    class_weight = _class_weights(labels, num_labels=num_labels, device=device)
    row_weights = []
    for row in labels:
        kept = row[row >= 0]
        non_o = kept[kept != 0]
        if non_o.numel() == 0:
            row_weights.append(torch.tensor(0.25, dtype=torch.float32, device=device))
        else:
            row_weights.append(class_weight[non_o].mean())
    return torch.stack(row_weights)


def _hidden_size(model: object) -> int:
    config = getattr(model, "config", None)
    hidden = getattr(config, "hidden_size", None)
    if hidden is None and isinstance(config, dict):
        hidden = config.get("hidden_size")
    if not isinstance(hidden, int):
        raise RuntimeError("CTLG encoder config does not expose integer hidden_size")
    return hidden


def _save_ctlg_artifact(
    *,
    encoder: object,
    cue_head: object,
    manifest: CTLGAdapterManifest,
    adapter_dir: Path,
) -> None:
    from safetensors.torch import save_file as save_safetensors

    adapter_dir.mkdir(parents=True, exist_ok=True)
    encoder.save_pretrained(str(adapter_dir / "lora_adapter"))  # type: ignore[attr-defined]
    save_safetensors(
        {
            "cue_head.weight": cue_head.weight.detach().cpu(),  # type: ignore[attr-defined]
            "cue_head.bias": cue_head.bias.detach().cpu(),  # type: ignore[attr-defined]
        },
        str(adapter_dir / "heads.safetensors"),
    )
    manifest.save(adapter_dir / "manifest.json")


def train(
    examples: list[CTLGExample],
    *,
    domain: Domain,
    adapter_dir: Path,
    manifest: CTLGAdapterManifest,
    epochs: int = 5,
    batch_size: int = 16,
    learning_rate: float = 3e-4,
    device: str | None = None,
    class_weighting: bool = True,
    balanced_sampling: bool = True,
) -> CTLGAdapterManifest:
    """Fine-tune the dedicated CTLG cue-tagger adapter.

    Loss is per-token cross entropy with ``ignore_index=-100`` for
    special tokens.  There are no pooled classification heads and no
    multi-task loss balancing.
    """
    manifest.validate()
    adapter_dir.mkdir(parents=True, exist_ok=True)
    device = device or _pick_device()

    try:
        import torch
        from peft import LoraConfig, get_peft_model  # type: ignore[import-not-found]
        from torch import nn
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "CTLG cue-tagger training requires torch, transformers, peft, and safetensors",
        ) from exc

    tokenizer = AutoTokenizer.from_pretrained(manifest.encoder, use_fast=True)
    encoder = AutoModel.from_pretrained(manifest.encoder)
    lora_cfg = LoraConfig(
        r=manifest.lora_r,
        lora_alpha=manifest.lora_alpha,
        lora_dropout=manifest.lora_dropout,
        target_modules=manifest.lora_target_modules,
        bias="none",
        task_type="FEATURE_EXTRACTION",
    )
    encoder = get_peft_model(encoder, lora_cfg)
    hidden = _hidden_size(encoder)
    cue_head = nn.Linear(hidden, len(manifest.cue_labels))
    encoder.to(device)
    cue_head.to(device)

    ds = _build_training_dataset(
        examples=examples,
        domain=domain,
        tokenizer=tokenizer,
        manifest=manifest,
    )
    if not ds.input_ids:
        raise RuntimeError(f"no CTLG examples for domain {domain!r}")
    input_ids, attention_mask, labels = _materialize_tensors(ds, device=device)

    weights = (
        _class_weights(labels, num_labels=len(manifest.cue_labels), device=device)
        if class_weighting
        else None
    )
    loss_fn = nn.CrossEntropyLoss(weight=weights, ignore_index=-100)
    optimizer = torch.optim.AdamW(
        [p for p in list(encoder.parameters()) + list(cue_head.parameters()) if p.requires_grad],
        lr=learning_rate,
    )
    sampling_weights = (
        _example_sampling_weights(labels, num_labels=len(manifest.cue_labels), device=device)
        if balanced_sampling
        else None
    )

    n = input_ids.size(0)
    final_avg_loss = float("nan")
    encoder.train()
    cue_head.train()
    for epoch in range(epochs):
        total_loss = 0.0
        if sampling_weights is None:
            perm = torch.randperm(n, device=device)
        else:
            perm = torch.multinomial(sampling_weights, n, replacement=True)
        for start in range(0, n, batch_size):
            batch_idx = perm[start : start + batch_size]
            sequence = encoder(
                input_ids=input_ids[batch_idx],
                attention_mask=attention_mask[batch_idx],
            ).last_hidden_state
            logits = cue_head(sequence)
            loss = loss_fn(logits.view(-1, len(manifest.cue_labels)), labels[batch_idx].view(-1))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach().cpu())
        final_avg_loss = total_loss / max((n + batch_size - 1) // batch_size, 1)
        logger.info("[ctlg] epoch %d/%d loss=%.4f", epoch + 1, epochs, final_avg_loss)

    encoder.eval()
    cue_head.eval()
    with torch.no_grad():
        sequence = encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        logits = cue_head(sequence)
        pred_ids = logits.argmax(dim=-1).detach().cpu().tolist()
    gold_labels: list[str] = []
    pred_labels: list[str] = []
    rows = zip(
        labels.detach().cpu().tolist(),
        pred_ids,
        ds.label_masks,
        strict=True,
    )
    for row_gold, row_pred, row_mask in rows:
        for gold_id, pred_id, keep in zip(row_gold, row_pred, row_mask, strict=True):
            if not keep or gold_id < 0:
                continue
            gold_labels.append(manifest.cue_labels[gold_id])
            pred_labels.append(manifest.cue_labels[pred_id])
    metrics = compute_cue_metrics(
        gold=gold_labels,
        pred=pred_labels,
        cue_labels=manifest.cue_labels,
    )

    manifest.trained_on = {
        "n_examples": n,
        "n_wordpieces": ds.n_wordpieces,
        "epochs": epochs,
        "class_weighting": int(class_weighting),
        "balanced_sampling": int(balanced_sampling),
    }
    manifest.trained_at = _dt.datetime.now(_dt.UTC).isoformat()
    manifest.corpus_hash = _corpus_hash(examples)
    manifest.gate_metrics = {
        **manifest.gate_metrics,
        "final_train_loss": round(final_avg_loss, 4),
        **{f"train_{k}": round(v, 4) for k, v in metrics.items()},
    }
    _save_ctlg_artifact(
        encoder=encoder,
        cue_head=cue_head,
        manifest=manifest,
        adapter_dir=adapter_dir,
    )
    return manifest


class LoraCTLGCueTagger:
    """Inference wrapper around a trained CTLG cue-tagger artifact."""

    name = "ctlg_lora_cue_tagger"

    def __init__(self, adapter_dir: Path, *, device: str | None = None) -> None:
        self._adapter_dir = adapter_dir
        self._manifest = verify_ctlg_adapter_dir(adapter_dir)
        self._device = device or _pick_device()
        self.adapter_domain = self._manifest.domain

        try:
            import torch
            from peft import PeftModel  # type: ignore[import-not-found]
            from safetensors.torch import load_file as load_safetensors
            from torch import nn
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "LoraCTLGCueTagger requires torch, transformers, peft, and safetensors",
            ) from exc

        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(self._manifest.encoder, use_fast=True)
        encoder = AutoModel.from_pretrained(self._manifest.encoder)
        encoder = PeftModel.from_pretrained(encoder, str(adapter_dir / "lora_adapter"))
        hidden = _hidden_size(encoder)
        cue_head = nn.Linear(hidden, len(self._manifest.cue_labels))
        state = load_safetensors(adapter_dir / "heads.safetensors")
        if "cue_head.weight" not in state or "cue_head.bias" not in state:
            raise CTLGAdapterIntegrityError("heads.safetensors missing cue_head weights")
        cue_head.load_state_dict(
            {
                "weight": state["cue_head.weight"],
                "bias": state["cue_head.bias"],
            },
        )
        self._encoder = encoder.to(self._device).eval()
        self._cue_head = cue_head.to(self._device).eval()

    @property
    def manifest(self) -> CTLGAdapterManifest:
        return self._manifest

    def extract_cues(self, text: str, *, domain: str = "") -> list[TaggedToken]:
        """Return CTLG cue tags for non-special tokenizer pieces."""
        if domain and domain != self._manifest.domain:
            logger.debug(
                "[ctlg] cross-domain call: adapter=%s request=%s",
                self._manifest.domain,
                domain,
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
        offsets = [tuple(p) for p in encoded["offset_mapping"][0].tolist()]

        with self._torch.no_grad():
            sequence = self._encoder(input_ids=input_ids, attention_mask=mask).last_hidden_state
            probs = self._torch.softmax(self._cue_head(sequence)[0], dim=-1)
            confs, ids = probs.max(dim=-1)

        return _decode_wordpiece_predictions(
            text=text,
            offsets=cast(list[tuple[int, int]], offsets),
            label_ids=[int(i) for i in ids.tolist()],
            confidences=[float(c) for c in confs.tolist()],
            cue_labels=self._manifest.cue_labels,
        )


def _decode_wordpiece_predictions(
    *,
    text: str,
    offsets: list[tuple[int, int]],
    label_ids: list[int],
    confidences: list[float],
    cue_labels: list[str],
) -> list[TaggedToken]:
    """Convert tokenizer-piece predictions into surface-token ``TaggedToken`` rows."""
    tokens: list[TaggedToken] = []
    previous_type: str | None = None
    for match in _SURFACE_TOKEN_RE.finditer(text):
        start, end = match.start(), match.end()
        label, confidence = _project_piece_predictions(
            token_offset=(start, end),
            piece_offsets=offsets,
            label_ids=label_ids,
            confidences=confidences,
            cue_labels=cue_labels,
        )
        label = _repair_predicted_label(label, previous_type)
        tokens.append(
            TaggedToken(
                char_start=start,
                char_end=end,
                surface=match.group(0),
                cue_label=cast(CueLabel, label),
                confidence=confidence,
            ),
        )
        previous_type = _cue_type(label)
    return tokens


def _project_piece_predictions(
    *,
    token_offset: tuple[int, int],
    piece_offsets: list[tuple[int, int]],
    label_ids: list[int],
    confidences: list[float],
    cue_labels: list[str],
) -> tuple[str, float]:
    start, end = token_offset
    best_non_o: tuple[str, float] | None = None
    best_any: tuple[str, float] = ("O", 0.0)
    for (piece_start, piece_end), label_id, confidence in zip(
        piece_offsets,
        label_ids,
        confidences,
        strict=False,
    ):
        if piece_start == piece_end:
            continue
        if label_id < 0 or label_id >= len(cue_labels):
            continue
        overlap = max(0, min(end, piece_end) - max(start, piece_start))
        if overlap <= 0:
            continue
        label = cue_labels[label_id]
        if confidence > best_any[1]:
            best_any = (label, confidence)
        if label != "O" and (best_non_o is None or confidence > best_non_o[1]):
            best_non_o = (label, confidence)
    return best_non_o or best_any


def _cue_type(label: str) -> str | None:
    if label == "O" or "-" not in label:
        return None
    return label.split("-", 1)[1]


def _repair_predicted_label(label: str, previous_type: str | None) -> str:
    cue_type = _cue_type(label)
    if cue_type is None:
        return "O"
    prefix = label.split("-", 1)[0]
    if prefix == "I" and previous_type != cue_type:
        return f"B-{cue_type}"
    if prefix not in {"B", "I"}:
        return "O"
    return label


__all__ = [
    "CTLGAdapterIntegrityError",
    "CTLGAdapterManifest",
    "LoraCTLGCueTagger",
    "_decode_wordpiece_predictions",
    "_build_training_dataset",
    "_example_sampling_weights",
    "load_ctlg_manifest",
    "compute_cue_metrics",
    "evaluate_cue_tagger",
    "train",
    "verify_ctlg_adapter_dir",
]
