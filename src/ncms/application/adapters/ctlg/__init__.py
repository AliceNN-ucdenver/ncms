"""Dedicated CTLG adapter support."""

from ncms.application.adapters.ctlg.audit import (
    CTLGFileAudit,
    CTLGGrammarAuditReport,
    CTLGGrammarMiss,
    audit_ctlg_files,
)
from ncms.application.adapters.ctlg.corpus import (
    CTLGCorpusError,
    CTLGDiagnostic,
    CTLGExample,
    CTLGExpectedQuery,
    CTLGValidationReport,
    CTLGVoice,
    dump_ctlg_jsonl,
    load_ctlg_jsonl,
    validate_ctlg_jsonl,
    validate_ctlg_row,
)
from ncms.application.adapters.ctlg.generator import (
    CTLGGenerationRequest,
    CTLGGenerationResult,
    LLMJsonCaller,
    generate_ctlg_examples,
    write_generation_result,
)
from ncms.application.adapters.ctlg.harness import (
    CTLGHarnessMode,
    CTLGHarnessResult,
    CuePayload,
    RetrieveLGFn,
    run_adapter_only,
    run_candidate_grounded_ctlg_shadow,
    run_ctlg_shadow,
    run_gold_cues,
    serialize_harness_result,
)
from ncms.application.adapters.ctlg.pilot import (
    CTLG_PILOT_PRESET_NAMES,
    CTLGPilotBatch,
    CTLGPilotDiagnostic,
    CTLGPilotRequest,
    CTLGPilotResult,
    apply_ctlg_pilot_preset,
    ctlg_pilot_preset_expectation,
    generate_ctlg_pilot,
    write_pilot_examples,
)
from ncms.application.adapters.ctlg.prompts import (
    CTLGPromptSpec,
    build_generation_prompt,
    build_judge_prompt,
)
from ncms.application.adapters.ctlg.sdg import (
    CTLGSDGRequest,
    CTLGSDGVoice,
    Segment,
    generate_ctlg_sdg_examples,
)
from ncms.application.adapters.ctlg.token_alignment import (
    WordpieceLabels,
    expand_bio_to_wordpieces,
)
from ncms.application.adapters.ctlg.training_corpus import (
    CTLGCorpusExclusion,
    CTLGTrainingCorpusBuild,
    build_ctlg_training_corpus,
)

_CUE_TAGGER_EXPORTS = {
    "CTLGAdapterIntegrityError",
    "CTLGAdapterManifest",
    "LoraCTLGCueTagger",
    "compute_cue_metrics",
    "evaluate_cue_tagger",
    "load_ctlg_manifest",
    "train",
    "verify_ctlg_adapter_dir",
}


def __getattr__(name: str) -> object:
    """Lazy-load cue tagger symbols to avoid a package import cycle."""
    if name in _CUE_TAGGER_EXPORTS:
        from ncms.application.adapters.methods import cue_tagger

        return getattr(cue_tagger, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "CTLGAdapterIntegrityError",
    "CTLGAdapterManifest",
    "CTLGCorpusError",
    "CTLGCorpusExclusion",
    "CTLGDiagnostic",
    "CTLGExample",
    "CTLGExpectedQuery",
    "CTLGFileAudit",
    "CTLGGenerationRequest",
    "CTLGGenerationResult",
    "CTLGGrammarAuditReport",
    "CTLGGrammarMiss",
    "CTLGHarnessMode",
    "CTLGHarnessResult",
    "CTLG_PILOT_PRESET_NAMES",
    "CTLGPilotBatch",
    "CTLGPilotDiagnostic",
    "CTLGPilotRequest",
    "CTLGPilotResult",
    "CTLGPromptSpec",
    "CTLGSDGRequest",
    "CTLGSDGVoice",
    "CTLGTrainingCorpusBuild",
    "CTLGValidationReport",
    "CTLGVoice",
    "CuePayload",
    "LLMJsonCaller",
    "LoraCTLGCueTagger",
    "RetrieveLGFn",
    "Segment",
    "WordpieceLabels",
    "apply_ctlg_pilot_preset",
    "audit_ctlg_files",
    "build_ctlg_training_corpus",
    "build_generation_prompt",
    "build_judge_prompt",
    "compute_cue_metrics",
    "ctlg_pilot_preset_expectation",
    "dump_ctlg_jsonl",
    "evaluate_cue_tagger",
    "expand_bio_to_wordpieces",
    "generate_ctlg_sdg_examples",
    "load_ctlg_manifest",
    "load_ctlg_jsonl",
    "generate_ctlg_examples",
    "generate_ctlg_pilot",
    "run_adapter_only",
    "run_candidate_grounded_ctlg_shadow",
    "run_ctlg_shadow",
    "run_gold_cues",
    "serialize_harness_result",
    "train",
    "validate_ctlg_jsonl",
    "validate_ctlg_row",
    "verify_ctlg_adapter_dir",
    "write_generation_result",
    "write_pilot_examples",
]
