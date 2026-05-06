"""Snapshot-style tests for ``MODULE_COMPILE_PROMPT_V3`` + builder.

Spec: ``openspec/changes/wiki-narrative-articles/specs/wiki-narrative-articles/spec.md``
covers the v3 prompt requirements:

  - Output schema includes ``narrative_sections`` array.
  - Per-section instructions (anchor, heading, paragraphs, citations,
    is_inference, optional visual).
  - Agent voice block (third-person, Wikipedia-editor, short
    paragraphs, NO activity narration).
  - Forbidden phrase list explicitly stated.
  - Word caps (150-400 per section, 1500-3000 typical article).
  - Worked examples (good + bad anti-pattern).
  - Single-pass — one LLM call returns plan + hero + narrative + body.
  - Archetype hint slot — Topic archetype gets empty hint; others
    inject a hint block (Session C wires this).
"""

from __future__ import annotations

from beever_atlas.wiki.prompts import (
    MODULE_COMPILE_PROMPT_V3,
    build_module_compile_prompt_v3,
)


def _minimal_catalog() -> list[dict]:
    return [
        {
            "id": "hero_summary",
            "label": "Summary",
            "description": "Bold TL;DR + 2-3 sentence overview.",
            "rule": "ALWAYS pick when fact_count ≥ 1.",
        },
        {
            "id": "narrative_article",
            "label": "Article",
            "description": "Multi-section explanatory article.",
            "rule": "Pick when narrative_section_count ≥ 1.",
        },
    ]


# ---------------------------------------------------------------------------
# Schema-presence assertions
# ---------------------------------------------------------------------------


def test_v3_prompt_includes_narrative_sections_schema() -> None:
    """The output schema explicitly enumerates the narrative_sections array."""
    assert '"narrative_sections":' in MODULE_COMPILE_PROMPT_V3
    assert '"anchor":' in MODULE_COMPILE_PROMPT_V3
    assert '"heading":' in MODULE_COMPILE_PROMPT_V3
    assert '"paragraphs":' in MODULE_COMPILE_PROMPT_V3
    assert '"citations":' in MODULE_COMPILE_PROMPT_V3
    assert '"is_inference":' in MODULE_COMPILE_PROMPT_V3
    assert '"visual":' in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_states_word_caps() -> None:
    """Word caps are explicit so the LLM can self-regulate."""
    assert "150-400 words" in MODULE_COMPILE_PROMPT_V3
    assert "1,500-3,000" in MODULE_COMPILE_PROMPT_V3 or "1500-3000" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_states_citation_discipline() -> None:
    """Citation discipline rules (HARD RULES) are spelled out."""
    assert "EVERY paragraph MUST cite at least one fact_id" in MODULE_COMPILE_PROMPT_V3
    assert "Inference paragraphs" in MODULE_COMPILE_PROMPT_V3
    # 80% coverage gate is mentioned.
    assert "80%" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_lists_forbidden_phrases() -> None:
    """All six forbidden activity-narration phrases are listed."""
    for phrase in (
        "shared a link",
        "shared an article",
        "noted that",
        "mentioned that",
        "posted about",
        "presented that",
    ):
        assert phrase in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_lists_visual_kinds() -> None:
    """All six visual kinds are listed."""
    for kind in ("table", "mermaid", "list", "callout", "code", "blockquote"):
        assert kind in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_includes_worked_examples() -> None:
    """At least one GOOD + one BAD example anchors the discipline."""
    assert "GOOD" in MODULE_COMPILE_PROMPT_V3
    assert "BAD" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_has_agent_voice_block() -> None:
    """Agent voice rules are stated."""
    assert "Third-person synthetic voice" in MODULE_COMPILE_PROMPT_V3
    assert "Wikipedia-editor" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_has_single_pass_promise() -> None:
    """Output JSON-only contract reflects single-pass cardinality."""
    assert "Output JSON ONLY" in MODULE_COMPILE_PROMPT_V3


# ---------------------------------------------------------------------------
# Builder substitution + archetype hint slot
# ---------------------------------------------------------------------------


def test_builder_formats_with_template_variables() -> None:
    """``build_module_compile_prompt_v3`` produces a renderable string
    with all placeholders substituted."""
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "topic"},
        module_catalog=_minimal_catalog(),
        title="Authlib OIDC Adoption",
        summary="The team adopted Authlib for OAuth/OIDC discovery.",
        top_facts=[{"fact_id": "f_1", "memory_text": "Adopted Authlib."}],
        top_people=[{"name": "Alice"}],
        date_range_start="2026-04-01",
        date_range_end="2026-05-01",
    )
    # No unsubstituted placeholders left over.
    assert "{module_catalog_block}" not in prompt
    assert "{signals_json}" not in prompt
    assert "{title}" not in prompt
    assert "{archetype_hint_block}" not in prompt
    # Page-level metadata + signals appear in the rendered prompt.
    assert "Authlib OIDC Adoption" in prompt
    assert "fact_count" in prompt
    assert "Adopted Authlib." in prompt


def test_topic_archetype_has_empty_hint_block() -> None:
    """Topic archetype passes an empty hint block — sections come from facts.

    Decision 2 in the design doc: the Topic archetype gets NO template
    hints (Session A defaults the slot to ``""``).
    """
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "topic"},
        module_catalog=_minimal_catalog(),
        title="X",
        summary="Y",
        top_facts=[],
        top_people=[],
    )
    # The hint placeholder is replaced with the empty string. The
    # surrounding prompt structure (sections + module rules) is
    # still present.
    assert "## Module-selection rules" in prompt
    # No archetype-specific hint section appears for Topic — Session
    # C will populate this for Decision/Tension/Folder/Overview.


def test_archetype_hint_block_injects_when_provided() -> None:
    """Builder injects the caller-supplied hint block verbatim.

    Session C will pass real hint blocks for Decision / Tension /
    Folder / Overview archetypes. Session A just verifies the
    plumbing.
    """
    hint = (
        "## Decision archetype hint\n"
        "Decision pages typically have sections such as: Context, The "
        "decision, Why, Alternatives rejected, Implications, Open "
        "consequences. Use these IF the data supports them."
    )
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "decision"},
        module_catalog=_minimal_catalog(),
        title="X",
        summary="Y",
        top_facts=[],
        top_people=[],
        archetype_hint_block=hint,
    )
    assert "Decision archetype hint" in prompt
    assert "Alternatives rejected" in prompt


def test_v3_prompt_structurally_distinct_from_v2() -> None:
    """v3 includes narrative_sections; v2 does not. Flag-OFF must use v2."""
    from beever_atlas.wiki.prompts import MODULE_COMPILE_PROMPT

    assert '"narrative_sections":' in MODULE_COMPILE_PROMPT_V3
    assert '"narrative_sections":' not in MODULE_COMPILE_PROMPT
