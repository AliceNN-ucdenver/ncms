"""Unit tests for the CTLG compositional synthesizer.

Tests each named rule in :mod:`ncms.domain.tlg.semantic_parser`
against crafted cue-tag inputs.  These tests are fast (pure
functions, no torch / no LLM) and are the main regression gate
for the synthesizer's rule ordering + match logic.
"""

from __future__ import annotations

from ncms.domain.tlg.cue_taxonomy import TaggedToken
from ncms.domain.tlg.semantic_parser import SLMQuerySignals, synthesize


def _mk_tokens(pairs: list[tuple[str, str]]) -> list[TaggedToken]:
    """Build a TaggedToken list from [(surface, label), ...].

    Offsets are synthetic; the synthesizer doesn't use character
    positions — only cue-family indexing — so offsets match
    surface length with a 1-char separator.
    """
    tokens: list[TaggedToken] = []
    pos = 0
    for surface, label in pairs:
        tokens.append(
            TaggedToken(
                char_start=pos,
                char_end=pos + len(surface),
                surface=surface,
                cue_label=label,  # type: ignore[arg-type]
                confidence=0.9,
            )
        )
        pos += len(surface) + 1
    return tokens


class TestCurrentState:
    def test_ask_current_plus_scope_routes_state_current(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("is", "O"),
                ("our", "O"),
                ("current", "B-ASK_CURRENT"),
                ("database", "B-SCOPE"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "current"
        assert q.scope == "database"
        assert q.matched_rule == "state_current"

    def test_ordinal_last_plus_scope_stays_ordinal(self) -> None:
        # "Our latest database" is an ordinal form.  Dispatch may map
        # it to the current walker, but the grammar should preserve
        # the CTLG relation instead of collapsing it to ASK_CURRENT.
        tokens = _mk_tokens(
            [
                ("Our", "O"),
                ("latest", "B-ORDINAL_LAST"),
                ("database", "B-SCOPE"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "ordinal"
        assert q.relation == "last"

    def test_ask_current_beats_latest_modifier(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "B-ASK_CURRENT"),
                ("is", "O"),
                ("the", "O"),
                ("latest", "B-ORDINAL_LAST"),
                ("chosen", "O"),
                ("approach", "B-SCOPE"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "current"
        assert q.scope == "approach"

    def test_bare_question_word_does_not_steal_ordinal_target(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "B-ASK_CURRENT"),
                ("context", "O"),
                ("was", "O"),
                ("described", "O"),
                ("first", "B-ORDINAL_FIRST"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "ordinal"
        assert q.relation == "first"

    def test_meaningful_current_cue_can_route_without_scope(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "B-ASK_CURRENT"),
                ("is", "O"),
                ("currently", "B-ASK_CURRENT"),
                ("adopted", "O"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "current"


class TestBeforeNamed:
    def test_before_plus_one_referent_routes_predecessor(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("did", "O"),
                ("we", "O"),
                ("use", "O"),
                ("before", "B-TEMPORAL_BEFORE"),
                ("Postgres", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "temporal"
        assert q.relation == "predecessor"
        assert q.referent == "postgres"
        assert q.matched_rule == "temporal_predecessor"

    def test_before_plus_two_referents_routes_binary_compare(self) -> None:
        tokens = _mk_tokens(
            [
                ("Did", "O"),
                ("OAuth", "B-REFERENT"),
                ("come", "O"),
                ("before", "B-TEMPORAL_BEFORE"),
                ("JWT", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "temporal"
        assert q.relation == "before_named"
        assert q.referent == "oauth"
        assert q.secondary == "jwt"
        assert q.matched_rule == "temporal_before_named"

    def test_before_without_referent_falls_to_during(self) -> None:
        # "before" alone without REFERENT doesn't fire before_named —
        # it also doesn't fire temporal_during because there's no
        # anchor.  Falls through.
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("was", "O"),
                ("before", "B-TEMPORAL_BEFORE"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        # Falls through to None → LLM.
        assert q is None or q.matched_rule != "temporal_before_named"


class TestCausal:
    def test_causal_explicit_plus_referent_routes_direct(self) -> None:
        tokens = _mk_tokens(
            [
                ("Why", "O"),
                ("did", "O"),
                ("we", "O"),
                ("use", "O"),
                ("Postgres", "B-REFERENT"),
                ("because", "B-CAUSAL_EXPLICIT"),
                ("of", "O"),
                ("scale", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "causal"
        assert q.relation == "cause_of"
        assert q.depth == 1
        assert q.referent == "postgres"
        assert q.matched_rule == "causal_direct"

    def test_multiword_altlex_routes_chain(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("led", "B-CAUSAL_ALTLEX"),
                ("to", "I-CAUSAL_ALTLEX"),
                ("the", "O"),
                ("Yugabyte", "B-REFERENT"),
                ("decision", "O"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.relation == "chain_cause_of"
        assert q.depth == 2
        assert q.referent == "yugabyte"
        assert q.matched_rule == "causal_chain"


class TestCounterfactual:
    def test_modal_routes_counterfactual(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("would", "B-MODAL_HYPOTHETICAL"),
                ("we", "O"),
                ("use", "O"),
                ("if", "O"),
                ("not", "O"),
                ("for", "O"),
                ("CockroachDB", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "modal"
        assert q.relation == "would_be_current_if"
        assert q.referent == "cockroachdb"
        assert q.scenario == "preserve_cockroachdb"
        assert q.matched_rule == "modal_counterfactual"

    def test_modal_trumps_other_rules(self) -> None:
        # A query with both MODAL and TEMPORAL_BEFORE should still
        # route to modal (specificity ordering).
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("would", "B-MODAL_HYPOTHETICAL"),
                ("we", "O"),
                ("have", "I-MODAL_HYPOTHETICAL"),
                ("used", "O"),
                ("before", "B-TEMPORAL_BEFORE"),
                ("Postgres", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "modal"


class TestOrdinal:
    def test_ordinal_first(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("was", "O"),
                ("our", "O"),
                ("first", "B-ORDINAL_FIRST"),
                ("database", "B-SCOPE"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "ordinal"
        assert q.relation == "first"
        assert q.scope == "database"

    def test_ordinal_target_beats_causal_relative_clause(self) -> None:
        tokens = _mk_tokens(
            [
                ("earliest", "B-ORDINAL_FIRST"),
                ("concern", "B-SCOPE"),
                ("led", "B-CAUSAL_ALTLEX"),
                ("to", "I-CAUSAL_ALTLEX"),
                ("decision", "B-REFERENT"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "ordinal"
        assert q.relation == "first"
        assert q.scope == "concern"


class TestTemporalDuring:
    def test_during_with_anchor(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("were", "O"),
                ("we", "O"),
                ("using", "O"),
                ("during", "B-TEMPORAL_DURING"),
                ("2023", "B-TEMPORAL_ANCHOR"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "temporal"
        assert q.relation == "during_interval"
        assert q.temporal_anchor == "2023"

    def test_during_named_referent_routes_concurrent(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("happened", "O"),
                ("during", "B-TEMPORAL_DURING"),
                ("OAuth", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "temporal"
        assert q.relation == "concurrent_with"
        assert q.referent == "oauth"


class TestAskChange:
    def test_slm_state_change_can_disambiguate_retirement(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("changed", "B-ASK_CHANGE"),
                ("for", "O"),
                ("Postgres", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(
            tokens,
            slm_signals=SLMQuerySignals(state_change="retirement"),
        )
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "retired"
        assert q.referent == "postgres"

    def test_retired_surface_disambiguates_without_slm(self) -> None:
        tokens = _mk_tokens(
            [
                ("Which", "O"),
                ("alternatives", "B-SCOPE"),
                ("were", "B-ASK_CHANGE"),
                ("retired", "I-ASK_CHANGE"),
                ("by", "O"),
                ("decision", "B-REFERENT"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "retired"
        assert q.scope == "alternatives"

    def test_retired_surface_without_cue_routes_alternatives_section(self) -> None:
        tokens = _mk_tokens(
            [
                ("Which", "O"),
                ("alternatives", "O"),
                ("were", "O"),
                ("retired", "O"),
                ("by", "O"),
                ("the", "O"),
                ("adopted", "O"),
                ("decision", "O"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "retired"
        assert q.scope == "alternatives"
        assert q.matched_rule == "alternatives_section"

    def test_considered_before_current_choice_routes_alternatives_section(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "B-ASK_CURRENT"),
                ("was", "O"),
                ("considered", "O"),
                ("before", "B-TEMPORAL_BEFORE"),
                ("the", "O"),
                ("current", "O"),
                ("choice", "O"),
                ("was", "O"),
                ("made", "O"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "state"
        assert q.relation == "retired"
        assert q.matched_rule == "alternatives_section"


class TestTraceSequence:
    def test_trace_from_context_routes_first_section(self) -> None:
        tokens = _mk_tokens(
            [
                ("Trace", "O"),
                ("the", "O"),
                ("decision", "O"),
                ("record", "O"),
                ("from", "B-TEMPORAL_BEFORE"),
                ("context", "O"),
                ("through", "O"),
                ("to", "O"),
                ("consequences", "O"),
                (".", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.axis == "ordinal"
        assert q.relation == "first"
        assert q.matched_rule == "trace_from_start"


class TestSLMGrounding:
    def test_slm_slots_ground_referent_and_scope_when_ctlg_marks_only_relation(self) -> None:
        tokens = _mk_tokens(
            [
                ("What", "O"),
                ("came", "O"),
                ("after", "B-TEMPORAL_AFTER"),
                ("it", "O"),
                ("?", "O"),
            ]
        )
        q = synthesize(
            tokens,
            slm_signals=SLMQuerySignals(slots={"framework": "Bulma"}),
        )
        assert q is not None
        assert q.relation == "after_named"
        assert q.referent == "bulma"
        assert q.scope == "framework"

    def test_slm_alternative_slot_grounds_secondary_argument(self) -> None:
        tokens = _mk_tokens(
            [
                ("Did", "O"),
                ("it", "B-REFERENT"),
                ("come", "O"),
                ("before", "B-TEMPORAL_BEFORE"),
                ("the", "O"),
                ("alternative", "O"),
                ("?", "O"),
            ]
        )
        q = synthesize(
            tokens,
            slm_signals=SLMQuerySignals(
                slots={"framework": "Bulma", "alternative": "Tailwind"},
            ),
        )
        assert q is not None
        assert q.relation == "before_named"
        assert q.referent == "it"
        assert q.secondary == "tailwind"


class TestFallback:
    def test_empty_tokens_returns_none(self) -> None:
        assert synthesize([]) is None

    def test_all_o_tokens_returns_none(self) -> None:
        tokens = _mk_tokens(
            [
                ("Thanks", "O"),
                ("for", "O"),
                ("the", "O"),
                ("info", "O"),
                (".", "O"),
            ]
        )
        assert synthesize(tokens) is None

    def test_lone_scope_returns_none(self) -> None:
        # Just "database" with no intent cue — not a TLG query.
        tokens = _mk_tokens(
            [
                ("database", "B-SCOPE"),
            ]
        )
        # No referent + no ask_current + no ordinal → no rule fires.
        assert synthesize(tokens) is None


class TestStateBareReferent:
    def test_referent_plus_scope_falls_to_state_at(self) -> None:
        # Minimal "was Postgres our database?" shape.
        tokens = _mk_tokens(
            [
                ("Was", "O"),
                ("Postgres", "B-REFERENT"),
                ("our", "O"),
                ("database", "B-SCOPE"),
                ("?", "O"),
            ]
        )
        q = synthesize(tokens)
        assert q is not None
        assert q.relation == "state_at"
        assert q.referent == "postgres"
        assert q.scope == "database"
        assert q.matched_rule == "state_bare_referent"
