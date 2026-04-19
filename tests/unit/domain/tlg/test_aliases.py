"""Unit tests for entity alias induction.

Pins the research behaviour:

* Abbreviation rule: short's letters match initials of full's words
  (hyphen + whitespace split).
* Short ``s`` suffix tolerated ("JWTs" ↔ "JSON Web Tokens").
* Bidirectional mapping.
* Empty corpus → empty dict.
* ``expand_aliases`` returns original + aliases, case-insensitive.
"""

from __future__ import annotations

from ncms.domain.tlg.aliases import (
    expand_aliases,
    induce_aliases,
)


class TestInduce:
    def test_basic_abbreviation(self) -> None:
        aliases = induce_aliases({"JWT", "JSON Web Tokens"})
        assert "JSON Web Tokens" in aliases["JWT"]
        assert "JWT" in aliases["JSON Web Tokens"]

    def test_hyphen_aware_initials(self) -> None:
        # "multi-factor authentication" → three words → MFA
        aliases = induce_aliases({"MFA", "multi-factor authentication"})
        assert "multi-factor authentication" in aliases["MFA"]

    def test_trailing_s_plural_still_matches(self) -> None:
        # "JWTs" abbreviates "JSON Web Tokens" — tolerated
        aliases = induce_aliases({"JWTs", "JSON Web Tokens"})
        assert "JSON Web Tokens" in aliases["JWTs"]

    def test_non_abbreviation_ignored(self) -> None:
        aliases = induce_aliases({"apple", "banana"})
        assert aliases == {}

    def test_ambiguous_initials_both_map(self) -> None:
        # "PT" matches both — expected: one short, two longs both alias.
        aliases = induce_aliases({"PT", "physical therapy", "project tracker"})
        assert "physical therapy" in aliases["PT"]
        assert "project tracker" in aliases["PT"]

    def test_single_word_long_form_rejected(self) -> None:
        # "api" vs "automation" — no multi-word form, no abbreviation
        aliases = induce_aliases({"api", "automation"})
        assert aliases == {}

    def test_corpus_requires_both_forms(self) -> None:
        # Only long form present → no alias registered.
        aliases = induce_aliases({"JSON Web Tokens"})
        assert aliases == {}

    def test_empty_input_yields_empty_dict(self) -> None:
        assert induce_aliases([]) == {}

    def test_whitespace_trimmed(self) -> None:
        aliases = induce_aliases({"  JWT  ", "JSON Web Tokens"})
        # Whitespace on the short form must not break matching; entities
        # are preserved as-is in the output.
        found = False
        for key, als in aliases.items():
            if "JWT" in key and "JSON Web Tokens" in als:
                found = True
        assert found


class TestExpandAliases:
    def test_returns_original_plus_aliases(self) -> None:
        aliases = induce_aliases({"JWT", "JSON Web Tokens"})
        expanded = expand_aliases("JWT", aliases)
        assert "JWT" in expanded
        assert "JSON Web Tokens" in expanded

    def test_case_insensitive_lookup(self) -> None:
        aliases = induce_aliases({"JWT", "JSON Web Tokens"})
        expanded = expand_aliases("jwt", aliases)
        assert "JSON Web Tokens" in expanded

    def test_unknown_entity_returns_itself_only(self) -> None:
        aliases = induce_aliases({"JWT", "JSON Web Tokens"})
        expanded = expand_aliases("something-else", aliases)
        assert expanded == frozenset({"something-else"})

    def test_empty_aliases_dict_passes_through(self) -> None:
        expanded = expand_aliases("anything", {})
        assert expanded == frozenset({"anything"})
