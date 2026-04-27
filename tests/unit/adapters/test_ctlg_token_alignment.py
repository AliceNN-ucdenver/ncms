"""Unit tests for CTLG surface-token to wordpiece label expansion."""

from __future__ import annotations

import pytest

from ncms.application.adapters.ctlg import expand_bio_to_wordpieces


def test_expand_bio_labels_to_wordpieces() -> None:
    result = expand_bio_to_wordpieces(
        word_labels=("B-REFERENT", "O", "B-CAUSAL_ALTLEX"),
        word_ids=[None, 0, 0, 1, 2, 2, None],
    )

    assert result.labels == (
        "O",
        "B-REFERENT",
        "I-REFERENT",
        "O",
        "B-CAUSAL_ALTLEX",
        "I-CAUSAL_ALTLEX",
        "O",
    )
    assert result.label_mask == (False, True, True, True, True, True, False)


def test_inside_label_stays_inside_on_split_word() -> None:
    result = expand_bio_to_wordpieces(
        word_labels=("I-REFERENT",),
        word_ids=[0, 0],
    )

    assert result.labels == ("I-REFERENT", "I-REFERENT")
    assert result.label_mask == (True, True)


def test_special_tokens_are_unmasked_o_labels() -> None:
    result = expand_bio_to_wordpieces(word_labels=("B-SUBJECT",), word_ids=[None, 0, None])

    assert result.labels == ("O", "B-SUBJECT", "O")
    assert result.label_mask == (False, True, False)


def test_invalid_word_id_raises() -> None:
    with pytest.raises(ValueError, match="outside"):
        expand_bio_to_wordpieces(word_labels=("O",), word_ids=[0, 1])
