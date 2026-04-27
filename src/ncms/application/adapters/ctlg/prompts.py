"""Prompt builders for CTLG cue-tag corpus generation and judging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ncms.domain.tlg.cue_taxonomy import CUE_LABELS

CTLGPromptVoice = Literal["query", "memory", "counterfactual"]


@dataclass(frozen=True)
class CTLGPromptSpec:
    """Configuration for one CTLG generation prompt."""

    domain: str
    voice: CTLGPromptVoice
    n_rows: int
    focus: str = ""
    examples: tuple[str, ...] = ()


_VOICE_INSTRUCTIONS: dict[CTLGPromptVoice, str] = {
    "query": (
        "Generate natural user questions that ask about temporal state, causal explanation, "
        "ordinal history, current state, or change over time."
    ),
    "memory": (
        "Generate stored memory statements that describe real observations, decisions, "
        "state transitions, causal drivers, enabling conditions, or temporal context."
    ),
    "counterfactual": (
        "Generate natural user questions with hypothetical or counterfactual framing, "
        "asking what would hold if a prior decision, cause, or transition had not happened. "
        "These are query-voice training rows; set the row voice field to query."
    ),
}


def _label_block() -> str:
    labels = "\n".join(f"- {label}" for label in CUE_LABELS)
    return "# Allowed BIO labels\n" + labels


def _tlg_query_contract_block() -> str:
    return "\n".join(
        [
            "# expected_tlg_query contract for query/counterfactual rows",
            "For query and counterfactual rows, include expected_tlg_query.",
            (
                "Use this exact object shape: {\"axis\": ..., \"relation\": ..., "
                "\"referent\": null|string, \"secondary\": null|string, "
                "\"subject\": null|string, \"scope\": null|string, \"depth\": 1, "
                "\"scenario\": null|string, \"temporal_anchor\": null|string}."
            ),
            "Use null only for fields that do not apply.",
            "Allowed axis values: temporal, causal, ordinal, modal, state.",
            (
                "Allowed relation values: state_at, before_named, after_named, between, "
                "concurrent_with, during_interval, predecessor, cause_of, effect_of, "
                "chain_cause_of, trigger_of, contributing_factor, first, last, nth, "
                "would_be_current_if, could_have_been, current, retired, declared."
            ),
            (
                "Examples: before one anchor -> relation=predecessor; X before Y -> "
                "relation=before_named with referent=X and secondary=Y; during a named "
                "event -> concurrent_with; during a date/period -> during_interval."
            ),
            (
                "Cue-to-query rule map: B-ASK_CURRENT -> axis=state relation=current; "
                "B-ORDINAL_LAST -> axis=ordinal relation=last; B-MODAL_HYPOTHETICAL -> "
                "axis=modal relation=would_be_current_if; B-TEMPORAL_BEFORE with one "
                "B-REFERENT -> axis=temporal relation=predecessor."
            ),
            (
                "Temporal before/after and modal rows must include a grounded domain "
                "referent such as Postgres, MySQL, Redis, Kafka, OAuth, REST, Kubernetes, "
                "or a similarly concrete technology/pattern. Do not use generic anchors "
                "like 'the release' as the only cue."
            ),
            (
                "Do not label bare question words like what/which/how as ASK_CURRENT; "
                "label present-state words such as current, currently, now, today, "
                "or at present."
            ),
            (
                "Label slot words like database, cache, broker, version, state, framework, "
                "or language as SCOPE, not REFERENT. If a SCOPE cue appears, "
                "expected_tlg_query.scope must contain that lowercase value."
            ),
            (
                "The expected_tlg_query must describe what the cue_tags will synthesize, "
                "not what the sentence loosely means."
            ),
        ]
    )


def _example_block(examples: tuple[str, ...]) -> str:
    if not examples:
        return ""
    rendered = "\n".join(f"- {example}" for example in examples)
    return "# Reference examples (style only; do not copy)\n" + rendered


def build_generation_prompt(spec: CTLGPromptSpec) -> str:
    """Build an LLM prompt that emits CTLG cue-tagged JSON rows."""
    if spec.n_rows <= 0:
        raise ValueError("n_rows must be positive")

    sections = [
        "# Task",
        (
            f"Generate exactly {spec.n_rows} CTLG cue-tagging rows for the "
            f"{spec.domain} domain."
        ),
        "# Voice",
        _VOICE_INSTRUCTIONS[spec.voice],
    ]
    if spec.focus:
        sections.extend(["# Focus", spec.focus.strip()])
    examples = _example_block(spec.examples)
    if examples:
        sections.append(examples)
    sections.extend(
        [
            _label_block(),
            _tlg_query_contract_block(),
            "# Row schema",
            (
                "Each row must be a JSON object with keys: text, tokens, cue_tags, "
                "domain, voice, split, source, note. Query/counterfactual rows must "
                "also include expected_tlg_query. tokens and cue_tags must have the "
                "same length. Use surface-word tokens, not BERT wordpieces. The voice "
                "field must be query or memory."
            ),
            "# Labeling rules",
            "\n".join(
                [
                    "- Every token gets exactly one label.",
                    "- Use O for tokens that are not CTLG cues.",
                    "- Use B-X to begin a cue span and I-X only to continue the same X span.",
                    "- Tag causal, temporal, ordinal, modal, referent, subject, and scope cues.",
                    "- Do not invent labels outside the allowed list.",
                    "- Keep rows natural; do not mention label names in the text.",
                    "- Ensure cue_tags synthesize to expected_tlg_query.",
                ],
            ),
            "# Output format",
            f"Return a JSON array of exactly {spec.n_rows} objects. No markdown fences.",
        ],
    )
    return "\n\n".join(sections)


def build_judge_prompt(row: dict) -> str:
    """Build a prompt for judging one CTLG-labeled row."""
    labels = row.get("cue_tags", [])
    tokens = row.get("tokens", [])
    token_lines = "\n".join(
        f"{idx}: {token!r} -> {label!r}"
        for idx, (token, label) in enumerate(zip(tokens, labels, strict=False))
    )
    return "\n\n".join(
        [
            "# Task",
            "Judge whether this CTLG cue-tagged row is valid training data.",
            _label_block(),
            _tlg_query_contract_block(),
            "# Row",
            f"TEXT: {row.get('text', '')!r}",
            f"DOMAIN: {row.get('domain', '')!r}",
            f"VOICE: {row.get('voice', '')!r}",
            "# Token labels",
            token_lines,
            "# Checks",
            "\n".join(
                [
                    "1. tokens exactly reconstruct the visible words/punctuation in text.",
                    "2. cue_tags length equals tokens length.",
                    "3. BIO transitions are legal.",
                    "4. cue labels are linguistically appropriate for the text.",
                    "5. query/memory/counterfactual voice matches the row.",
                    "6. query rows include expected_tlg_query and cue_tags synthesize to it.",
                ],
            ),
            "# Output",
            (
                "Return only JSON: {\"verdict\":\"valid\"|\"fixable\"|\"invalid\", "
                "\"issues\":[...], \"suggested_cue_tags\":[...]}. "
                "Use suggested_cue_tags only when verdict is fixable."
            ),
        ],
    )


__all__ = ["CTLGPromptSpec", "build_generation_prompt", "build_judge_prompt"]
