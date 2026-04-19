"""Unit tests for the TLG retirement extractor.

Covers the three retirement-pattern shapes (active / passive /
directional), the ``dst_new`` filter, the ``MID`` cross-reference
exclusion, the domain-noun exclusion, and the seed-verb inventory.

These tests pin the structural contract the extractor enforces;
Phase 2 induction will replace the seed inventory with corpus-mined
markers but the extraction rules themselves should not move.
"""

from __future__ import annotations

from ncms.domain.tlg.retirement_extractor import (
    SEED_RETIREMENT_VERBS,
    extract_retired,
)

# ---------------------------------------------------------------------------
# Active pattern: ``<verb> <NP>`` (imperative)
# ---------------------------------------------------------------------------


class TestActivePattern:
    def test_retire_verb_names_post_verb_entity(self) -> None:
        # "Retire long-lived JWTs, adopt short-lived session tokens."
        retired = extract_retired(
            dst_content="Retire long-lived JWTs, adopt short-lived session tokens.",
            src_entities=frozenset({"long-lived JWTs"}),
            dst_entities=frozenset({"session tokens"}),
        )
        assert "long-lived JWTs" in retired

    def test_deprecate_verb_fires(self) -> None:
        retired = extract_retired(
            dst_content="Deprecate basic-auth flow entirely.",
            src_entities=frozenset({"basic-auth flow"}),
            dst_entities=frozenset(),
        )
        assert "basic-auth flow" in retired

    def test_replace_verb_fires(self) -> None:
        # "SHA-1" / "SHA-256" would match the MID-reference filter
        # (``[A-Z]{2,5}-\w*``) which is intentional — ADR-021 style
        # doc refs get dropped.  Use names that don't collide.
        retired = extract_retired(
            dst_content="Replace legacy password hashing with bcrypt across the signing stack.",
            src_entities=frozenset({"legacy password hashing"}),
            dst_entities=frozenset({"bcrypt"}),
        )
        assert "legacy password hashing" in retired


# ---------------------------------------------------------------------------
# Passive pattern: ``<NP> (is|was|are|were) <verb>``
# ---------------------------------------------------------------------------


class TestPassivePattern:
    def test_passive_pre_verb_entity_is_retired(self) -> None:
        retired = extract_retired(
            dst_content="Session cookies are fully retired.",
            src_entities=frozenset({"session cookies"}),
            dst_entities=frozenset(),
        )
        assert "session cookies" in retired

    def test_passive_with_was(self) -> None:
        retired = extract_retired(
            dst_content="MD5 was deprecated last quarter.",
            src_entities=frozenset({"MD5"}),
            dst_entities=frozenset(),
        )
        assert "MD5" in retired


# ---------------------------------------------------------------------------
# Directional pattern: ``moves/migrates from X to Y`` — only X retires.
# ---------------------------------------------------------------------------


class TestDirectionalPattern:
    def test_from_side_retired_to_side_not(self) -> None:
        retired = extract_retired(
            dst_content="Authentication moves from session cookies to OAuth 2.0.",
            src_entities=frozenset({"session cookies"}),
            dst_entities=frozenset({"OAuth 2.0"}),
        )
        assert "session cookies" in retired
        assert "OAuth 2.0" not in retired

    def test_moves_to_without_from_skips_sentence(self) -> None:
        # "moves to X" alone names only the new state — no retirement
        # can be inferred.
        retired = extract_retired(
            dst_content="The team moves to Kubernetes.",
            src_entities=frozenset({"Docker Swarm"}),
            dst_entities=frozenset({"Kubernetes"}),
        )
        # Docker Swarm is not in the content, so it can still be caught
        # by the set-diff tail — but the directional verb must not fire.
        # Here the set-diff *does* catch Docker Swarm because it was in
        # src and not dst.  That's the expected behavior (set-diff is
        # the safety net for silent disappearances).
        assert "Docker Swarm" in retired

    def test_migrate_directional(self) -> None:
        retired = extract_retired(
            dst_content="Backend migrates from REST to gRPC.",
            src_entities=frozenset({"REST"}),
            dst_entities=frozenset({"gRPC"}),
        )
        assert "REST" in retired


# ---------------------------------------------------------------------------
# Filters: MID-like refs, domain nouns, dst_new
# ---------------------------------------------------------------------------


class TestExclusionFilters:
    def test_adr_style_mid_references_dropped(self) -> None:
        # "ADR-021" is a cross-reference, not a retired entity.
        retired = extract_retired(
            dst_content="Retire ADR-021 decision and ADR-050 fallback.",
            src_entities=frozenset({"ADR-021", "ADR-050"}),
            dst_entities=frozenset(),
        )
        assert retired == frozenset()

    def test_domain_entity_excluded_from_retirement(self) -> None:
        # "authentication" is a domain noun (topic, not a state).
        retired = extract_retired(
            dst_content="Retire authentication session cookies.",
            src_entities=frozenset({"authentication", "session cookies"}),
            dst_entities=frozenset(),
            domain_entities=frozenset({"authentication"}),
        )
        assert "session cookies" in retired
        assert "authentication" not in retired

    def test_dst_new_filter_drops_introductions(self) -> None:
        # A verb-sentence may mention both old and new — the new side
        # (appearing only in dst) must not be marked as retired.
        # Note: arthroscopic surgery isn't in src, so even if the verb
        # scan picks it up post-verb, the dst_new filter drops it.
        retired = extract_retired(
            dst_content="Retire open surgery, replace with arthroscopic surgery scheduled for Q2.",
            src_entities=frozenset({"open surgery"}),
            dst_entities=frozenset({"arthroscopic surgery"}),
        )
        assert "open surgery" in retired
        assert "arthroscopic surgery" not in retired


# ---------------------------------------------------------------------------
# Set-diff safety net
# ---------------------------------------------------------------------------


class TestSetDiffTail:
    def test_silent_disappearance_caught_by_setdiff(self) -> None:
        # No retirement verb fires in the content — but an entity was
        # in src and not dst, so set-diff catches the silent drop.
        retired = extract_retired(
            dst_content="The new configuration uses feature flag X.",
            src_entities=frozenset({"legacy-config"}),
            dst_entities=frozenset({"feature flag X"}),
        )
        assert "legacy-config" in retired

    def test_setdiff_still_respects_domain_exclusion(self) -> None:
        retired = extract_retired(
            dst_content="No verb hit here.",
            src_entities=frozenset({"authentication"}),
            dst_entities=frozenset(),
            domain_entities=frozenset({"authentication"}),
        )
        assert "authentication" not in retired


# ---------------------------------------------------------------------------
# Custom verb inventory (simulates Phase 2 marker induction output)
# ---------------------------------------------------------------------------


class TestCustomVerbInventory:
    def test_custom_verbs_override_seed(self) -> None:
        # Pass an empty verb set — no structural pattern should fire.
        retired = extract_retired(
            dst_content="Retire long-lived JWTs.",
            src_entities=frozenset({"long-lived JWTs"}),
            dst_entities=frozenset(),
            retirement_verbs=frozenset(),
        )
        # With no verbs, only set-diff runs — long-lived JWTs isn't in
        # dst, so set-diff catches it.
        assert "long-lived JWTs" in retired

    def test_additional_verb_extends_seed_effectively(self) -> None:
        # A Phase-2 induction might add "sunset" as a retirement verb.
        retired = extract_retired(
            dst_content="Sunset the legacy API gateway next quarter.",
            src_entities=frozenset({"legacy API gateway"}),
            dst_entities=frozenset(),
            retirement_verbs=SEED_RETIREMENT_VERBS | {"sunset"},
        )
        assert "legacy API gateway" in retired


# ---------------------------------------------------------------------------
# No-op / boundary cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_content_returns_empty(self) -> None:
        retired = extract_retired(
            dst_content="",
            src_entities=frozenset({"foo"}),
            dst_entities=frozenset({"foo"}),
        )
        assert retired == frozenset()

    def test_identical_entity_sets_no_setdiff(self) -> None:
        retired = extract_retired(
            dst_content="No verbs in this sentence at all.",
            src_entities=frozenset({"foo", "bar"}),
            dst_entities=frozenset({"foo", "bar"}),
        )
        assert retired == frozenset()

    def test_returns_frozenset_type(self) -> None:
        retired = extract_retired(
            dst_content="",
            src_entities=frozenset(),
            dst_entities=frozenset(),
        )
        assert isinstance(retired, frozenset)
