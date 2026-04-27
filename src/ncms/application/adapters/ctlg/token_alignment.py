"""Wordpiece alignment helpers for CTLG cue-tag training."""

from __future__ import annotations

from dataclasses import dataclass

from ncms.domain.tlg.cue_taxonomy import CueLabel


@dataclass(frozen=True)
class WordpieceLabels:
    """BIO labels aligned to tokenizer wordpieces."""

    labels: tuple[CueLabel, ...]
    label_mask: tuple[bool, ...]


def _to_inside(label: CueLabel) -> CueLabel:
    if label.startswith("B-"):
        return ("I-" + label[2:])  # type: ignore[return-value]
    return label


def expand_bio_to_wordpieces(
    *,
    word_labels: tuple[CueLabel, ...],
    word_ids: list[int | None],
) -> WordpieceLabels:
    """Expand surface-word BIO labels to tokenizer wordpieces.

    ``word_ids`` is the Hugging Face fast-tokenizer alignment output:
    special tokens are ``None`` and regular wordpieces point at the
    source word index.  The first wordpiece keeps the word label; later
    wordpieces for the same word receive the corresponding ``I-X`` label.
    Special tokens get ``O`` and ``label_mask=False`` so training can
    ignore them.
    """
    labels: list[CueLabel] = []
    mask: list[bool] = []
    previous_word_id: int | None = None
    for word_id in word_ids:
        if word_id is None:
            labels.append("O")
            mask.append(False)
            previous_word_id = None
            continue
        if word_id < 0 or word_id >= len(word_labels):
            raise ValueError(f"word_id {word_id} outside {len(word_labels)} labels")
        label = word_labels[word_id]
        if word_id == previous_word_id:
            label = _to_inside(label)
        labels.append(label)
        mask.append(True)
        previous_word_id = word_id
    return WordpieceLabels(labels=tuple(labels), label_mask=tuple(mask))


__all__ = ["WordpieceLabels", "expand_bio_to_wordpieces"]
