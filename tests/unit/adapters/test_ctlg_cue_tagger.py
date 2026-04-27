"""Unit tests for the dedicated CTLG cue-tagger artifact skeleton."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncms.application.adapters.ctlg import (
    CTLGAdapterIntegrityError,
    CTLGAdapterManifest,
    CTLGExample,
    compute_cue_metrics,
    evaluate_cue_tagger,
    load_ctlg_manifest,
    verify_ctlg_adapter_dir,
)
from ncms.application.adapters.methods.cue_tagger import (
    _build_training_dataset,
    _class_weights,
    _decode_wordpiece_predictions,
    _example_sampling_weights,
)


def _write_artifact(
    root: Path,
    *,
    manifest: CTLGAdapterManifest | None = None,
    heads: bytes = b"not-empty",
) -> Path:
    adapter_dir = root / "software_dev" / "ctlg-v1"
    (adapter_dir / "lora_adapter").mkdir(parents=True)
    (adapter_dir / "lora_adapter" / "adapter_config.json").write_text("{}")
    (adapter_dir / "heads.safetensors").write_bytes(heads)
    (manifest or CTLGAdapterManifest(domain="software_dev")).save(adapter_dir / "manifest.json")
    return adapter_dir


def test_manifest_round_trips_and_drops_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = CTLGAdapterManifest(domain="software_dev", version="ctlg-v2", corpus_hash="abc")
    manifest.save(path)
    raw = json.loads(path.read_text())
    raw["future_field"] = "ignored"
    path.write_text(json.dumps(raw))

    loaded = CTLGAdapterManifest.load(path)

    assert loaded.domain == "software_dev"
    assert loaded.version == "ctlg-v2"
    assert loaded.corpus_hash == "abc"


def test_manifest_rejects_unknown_cue_labels(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    CTLGAdapterManifest(cue_labels=["O", "B-NOT_REAL"]).save(path)

    with pytest.raises(CTLGAdapterIntegrityError, match="unknown labels"):
        CTLGAdapterManifest.load(path)


def test_verify_ctlg_adapter_dir_accepts_valid_layout(tmp_path: Path) -> None:
    adapter_dir = _write_artifact(tmp_path)

    manifest = verify_ctlg_adapter_dir(adapter_dir)

    assert manifest.domain == "software_dev"
    assert manifest.version == "ctlg-v1"


def test_verify_ctlg_adapter_dir_rejects_missing_heads(tmp_path: Path) -> None:
    adapter_dir = _write_artifact(tmp_path)
    (adapter_dir / "heads.safetensors").unlink()

    with pytest.raises(CTLGAdapterIntegrityError, match="heads.safetensors"):
        verify_ctlg_adapter_dir(adapter_dir)


def test_verify_ctlg_adapter_dir_rejects_missing_lora_config(tmp_path: Path) -> None:
    adapter_dir = _write_artifact(tmp_path)
    (adapter_dir / "lora_adapter" / "adapter_config.json").unlink()

    with pytest.raises(CTLGAdapterIntegrityError, match="adapter_config"):
        verify_ctlg_adapter_dir(adapter_dir)


def test_load_manifest_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(CTLGAdapterIntegrityError, match="manifest.json"):
        load_ctlg_manifest(tmp_path)


def test_decode_wordpiece_predictions_skips_special_tokens_and_duplicates() -> None:
    tokens = _decode_wordpiece_predictions(
        text="Why Postgres?",
        offsets=[(0, 0), (0, 3), (4, 12), (4, 12), (12, 13), (0, 0)],
        label_ids=[0, 1, 2, 2, 0, 0],
        confidences=[1.0, 0.9, 0.8, 0.7, 0.6, 1.0],
        cue_labels=["O", "B-CAUSAL_EXPLICIT", "B-REFERENT"],
    )

    assert [t.surface for t in tokens] == ["Why", "Postgres", "?"]
    assert [t.cue_label for t in tokens] == ["B-CAUSAL_EXPLICIT", "B-REFERENT", "O"]
    assert tokens[1].confidence == 0.8


def test_decode_wordpiece_predictions_projects_to_surface_tokens() -> None:
    tokens = _decode_wordpiece_predictions(
        text="Use MySQL?",
        offsets=[(0, 0), (0, 3), (4, 6), (6, 7), (7, 8), (8, 9), (9, 10), (0, 0)],
        label_ids=[0, 0, 0, 2, 2, 2, 0, 0],
        confidences=[1.0, 0.9, 0.8, 0.7, 0.75, 0.72, 0.95, 1.0],
        cue_labels=["O", "B-REFERENT", "I-REFERENT"],
    )

    assert [t.surface for t in tokens] == ["Use", "MySQL", "?"]
    assert [t.cue_label for t in tokens] == ["O", "B-REFERENT", "O"]
    assert tokens[1].confidence == 0.75


class _FakeEncoding(dict):
    def __init__(
        self,
        *,
        input_ids: list[int],
        attention_mask: list[int],
        word_ids: list[int | None],
    ):
        super().__init__(input_ids=input_ids, attention_mask=attention_mask)
        self._word_ids = word_ids

    def word_ids(self):
        return self._word_ids


class _FakeTokenizer:
    def __call__(self, tokens, **kwargs):
        assert kwargs["is_split_into_words"] is True
        return _FakeEncoding(
            input_ids=[101, 10, 11, 12, 13, 102],
            attention_mask=[1, 1, 1, 1, 1, 1],
            word_ids=[None, 0, 0, 1, 2, None],
        )


def test_build_training_dataset_expands_bio_to_wordpieces() -> None:
    ex = CTLGExample(
        text="Postgres replaced MySQL",
        tokens=("Postgres", "replaced", "MySQL"),
        cue_tags=("B-REFERENT", "O", "B-REFERENT"),
        char_offsets=((0, 8), (9, 17), (18, 23)),
        domain="software_dev",
        voice="memory",
        split="train",
    )
    manifest = CTLGAdapterManifest(
        domain="software_dev",
        cue_labels=["O", "B-REFERENT", "I-REFERENT"],
        max_length=6,
    )

    ds = _build_training_dataset(
        examples=[ex],
        domain="software_dev",
        tokenizer=_FakeTokenizer(),
        manifest=manifest,
    )

    assert ds.input_ids == [[101, 10, 11, 12, 13, 102]]
    assert ds.labels == [[-100, 1, 2, 0, 1, -100]]
    assert ds.label_masks == [[False, True, True, True, True, False]]
    assert ds.n_wordpieces == 4


def test_build_training_dataset_filters_domain() -> None:
    ex = CTLGExample(
        text="x",
        tokens=("x",),
        cue_tags=("O",),
        char_offsets=((0, 1),),
        domain="clinical",
        voice="memory",
        split="train",
    )
    ds = _build_training_dataset(
        examples=[ex],
        domain="software_dev",
        tokenizer=_FakeTokenizer(),
        manifest=CTLGAdapterManifest(domain="software_dev"),
    )

    assert ds.input_ids == []


def test_class_weights_downweights_dominant_o_label() -> None:
    torch = pytest.importorskip("torch")
    labels = torch.tensor([[0, 0, 0, 1, -100]], dtype=torch.long)

    weights = _class_weights(labels, num_labels=3, device="cpu")

    assert weights[0] < weights[1]
    assert weights[2] == 1.0


def test_example_sampling_weights_boost_rows_with_rare_cues() -> None:
    torch = pytest.importorskip("torch")
    labels = torch.tensor(
        [
            [0, 0, 0, -100],
            [0, 1, 1, -100],
            [0, 2, -100, -100],
        ],
        dtype=torch.long,
    )

    weights = _example_sampling_weights(labels, num_labels=3, device="cpu")

    assert weights[1] > weights[0]
    assert weights[2] > weights[0]


def test_compute_cue_metrics_excludes_o_from_macro() -> None:
    metrics = compute_cue_metrics(
        gold=["O", "B-REFERENT", "B-REFERENT", "B-CAUSAL_EXPLICIT"],
        pred=["O", "B-REFERENT", "O", "B-REFERENT"],
        cue_labels=["O", "B-REFERENT", "B-CAUSAL_EXPLICIT"],
    )

    assert metrics["token_accuracy"] == 0.5
    assert metrics["token_micro_f1"] == 0.5
    assert round(metrics["non_o_macro_f1"], 4) == 0.25
    assert round(metrics["family_referent_f1"], 4) == 0.5
    assert metrics["family_causal_explicit_f1"] == 0.0
    assert round(metrics["label_b_referent_f1"], 4) == 0.5


def test_compute_cue_metrics_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="equal length"):
        compute_cue_metrics(gold=["O"], pred=[])


class _FakeCueTagger:
    def extract_cues(self, text: str, *, domain: str = ""):
        assert domain == "software_dev"
        return [
            type(
                "Pred",
                (),
                {
                    "char_start": 0,
                    "char_end": 8,
                    "cue_label": "B-REFERENT",
                },
            )(),
            type(
                "Pred",
                (),
                {
                    "char_start": 18,
                    "char_end": 23,
                    "cue_label": "B-REFERENT",
                },
            )(),
        ]


def test_evaluate_cue_tagger_projects_predictions_to_gold_offsets() -> None:
    ex = CTLGExample(
        text="Postgres replaced MySQL",
        tokens=("Postgres", "replaced", "MySQL"),
        cue_tags=("B-REFERENT", "O", "B-REFERENT"),
        char_offsets=((0, 8), (9, 17), (18, 23)),
        domain="software_dev",
        voice="memory",
        split="test",
    )

    metrics = evaluate_cue_tagger(_FakeCueTagger(), [ex], domain="software_dev")

    assert metrics["token_accuracy"] == 1.0
    assert metrics["non_o_macro_f1"] == 1.0
    assert metrics["n_examples"] == 1.0
    assert metrics["n_tokens"] == 3.0


def test_evaluate_cue_tagger_requires_extract_cues() -> None:
    with pytest.raises(TypeError, match="extract_cues"):
        evaluate_cue_tagger(object(), [], domain="software_dev")
