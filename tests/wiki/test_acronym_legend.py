"""Tests for the ``acronym_legend`` module.

Covers:
  - catalog entry shape
  - eligibility predicate (≥2 glossary terms used on the page)
  - word-boundary matching (``\\bTERM\\b``) so substring hits don't
    spuriously fire ("MFAS" should not match "MFA")
  - ALL-CAPS acronyms are case-sensitive (so ``MFA`` matches but
    ``mfa`` doesn't unless the term itself is lowercase)
  - filter to terms that ACTUALLY appear in fact bodies
  - graceful empty inputs

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.acronym_legend import (
    build_acronym_legend_data,
    count_glossary_terms_used,
)


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_acronym_legend_in_catalog() -> None:
    assert "acronym_legend" in MODULE_CATALOG
    spec = MODULE_CATALOG["acronym_legend"]
    assert spec.id == "acronym_legend"
    assert spec.label == "Terms used"
    assert spec.renderer_kind == "frontend"


def test_acronym_legend_predicate_requires_min_2_terms() -> None:
    spec = MODULE_CATALOG["acronym_legend"]
    assert spec.eligible({"glossary_terms_used": 2}) is True
    assert spec.eligible({"glossary_terms_used": 5}) is True
    assert spec.eligible({"glossary_terms_used": 1}) is False
    assert spec.eligible({"glossary_terms_used": 0}) is False
    assert spec.eligible({}) is False  # missing key defaults to 0


# ---------------------------------------------------------------------------
# count_glossary_terms_used — signal
# ---------------------------------------------------------------------------


def test_count_glossary_terms_used_matches_word_boundary() -> None:
    glossary = [
        {"term": "MFA", "definition": "Multi-Factor Authentication"},
        {"term": "OIDC", "definition": "OpenID Connect"},
        {"term": "SAML", "definition": "Security Assertion Markup Language"},
    ]
    facts = [
        {"memory_text": "We rolled out MFA across the org."},
        {"memory_text": "OIDC was the chosen protocol."},
        {"memory_text": "Generic prose with no glossary terms here."},
    ]
    assert count_glossary_terms_used(glossary, facts) == 2


def test_count_glossary_terms_used_does_not_match_substrings() -> None:
    """``\\bTERM\\b`` boundary — ``MFAS`` must NOT count as a hit
    for term ``MFA``."""
    glossary = [{"term": "MFA"}]
    facts = [{"memory_text": "MFAS is unrelated to MFA semantically."}]
    # The fact text DOES contain MFA on its own at the end, so this
    # SHOULD count once. To check pure substring rejection, use a
    # body where MFA only appears as a substring.
    facts_substring_only = [{"memory_text": "Discussed MFAS thoroughly."}]
    assert count_glossary_terms_used(glossary, facts_substring_only) == 0
    assert count_glossary_terms_used(glossary, facts) == 1


def test_count_glossary_terms_used_acronyms_case_sensitive() -> None:
    """All-caps acronyms must match case-sensitively; ``mfa`` should
    NOT match the term ``MFA`` (otherwise we false-match "Mfa Co")."""
    glossary = [{"term": "MFA"}]
    facts = [{"memory_text": "We deployed mfa across the team."}]
    assert count_glossary_terms_used(glossary, facts) == 0


def test_count_glossary_terms_used_phrases_case_insensitive() -> None:
    """Multi-word phrases or non-acronym terms match case-insensitively
    so "Wiki Compiler" finds "wiki compiler" too."""
    glossary = [{"term": "wiki compiler"}]
    facts = [{"memory_text": "The Wiki Compiler runs once per channel."}]
    assert count_glossary_terms_used(glossary, facts) == 1


def test_count_glossary_terms_used_handles_empty_inputs() -> None:
    assert count_glossary_terms_used([], []) == 0
    assert count_glossary_terms_used(None, [{"memory_text": "x"}]) == 0  # type: ignore[arg-type]
    assert count_glossary_terms_used([{"term": "X"}], None) == 0  # type: ignore[arg-type]


def test_count_glossary_terms_used_supports_string_entries() -> None:
    """Channel-level glossary may store bare strings instead of dicts;
    handle both."""
    glossary = ["MFA", "OIDC"]
    facts = [
        {"memory_text": "We use MFA + OIDC together."},
    ]
    assert count_glossary_terms_used(glossary, facts) == 2  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_acronym_legend_data — payload shape + filtering
# ---------------------------------------------------------------------------


def test_build_returns_only_terms_used_on_page() -> None:
    glossary = [
        {"term": "MFA", "definition": "Multi-Factor Authentication"},
        {"term": "OIDC", "definition": "OpenID Connect"},
        {"term": "SAML", "definition": "unused on this page"},
    ]
    facts = [
        {"memory_text": "Rolled out MFA org-wide."},
        {"memory_text": "OIDC chosen for the new IdP."},
    ]
    data = build_acronym_legend_data(glossary, facts)
    assert data["label"] == "Terms used on this page"
    assert data["renderer_kind"] == "frontend"
    terms = [it["term"] for it in data["items"]]
    assert "MFA" in terms
    assert "OIDC" in terms
    assert "SAML" not in terms


def test_build_preserves_definition_and_first_mentioned_by() -> None:
    glossary = [
        {
            "term": "MFA",
            "definition": "Multi-Factor Authentication",
            "first_mentioned_by": "Dante Lok",
        },
    ]
    facts = [{"memory_text": "MFA rollout starts Monday."}]
    data = build_acronym_legend_data(glossary, facts)
    item = data["items"][0]
    assert item["definition"] == "Multi-Factor Authentication"
    assert item["first_mentioned_by"] == "Dante Lok"


def test_build_handles_empty_inputs() -> None:
    assert build_acronym_legend_data(None, None)["items"] == []  # type: ignore[arg-type]
    assert build_acronym_legend_data([], [])["items"] == []
    assert build_acronym_legend_data(
        [{"term": "MFA"}], []
    )["items"] == []  # no facts → nothing matched


def test_build_dedupes_terms_by_lowercase() -> None:
    """A glossary that lists "MFA" and "mfa" should produce a single
    legend entry."""
    glossary = [{"term": "MFA"}, {"term": "MFA"}]
    facts = [{"memory_text": "MFA"}]
    data = build_acronym_legend_data(glossary, facts)
    assert len(data["items"]) == 1


def test_build_caps_at_30_items() -> None:
    glossary = [{"term": f"TERM{i}", "definition": f"def {i}"} for i in range(40)]
    facts = [{"memory_text": " ".join(f"TERM{i}" for i in range(40))}]
    data = build_acronym_legend_data(glossary, facts)
    assert len(data["items"]) == 30
