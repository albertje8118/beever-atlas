"""Tests for the planner support functions:
- ``compute_signals`` — pure aggregation of cluster data
- ``_validate_plan`` — drops modules whose eligibility fails

The orchestrator runs the actual LLM call (single unified prompt).
The end-to-end LLM-call flow is covered in
``test_modules_orchestrator.py``.
"""

from __future__ import annotations

from beever_atlas.wiki.modules.planner import (
    ModulePlan,
    _validate_plan,
    compute_signals,
)


# ---------------------------------------------------------------------------
# compute_signals — pure aggregation
# ---------------------------------------------------------------------------


def test_compute_signals_counts_facts_and_decisions() -> None:
    cluster = {
        "title": "Auth",
        "member_facts": [
            {"fact_type": "event", "date": "2026-04-01"},
            {"fact_type": "event", "date": "2026-04-15"},
            {"fact_type": "decision", "date": "2026-04-20"},
            {"fact_type": "claim"},
            {"fact_type": "claim"},
        ],
    }
    signals = compute_signals(
        cluster=cluster,
        decisions=[{"decision": "Adopt JWT"}, {"decision": "Drop SAML"}],
    )
    assert signals["fact_count"] == 5
    assert signals["decision_count"] == 2
    assert signals["event_count"] == 3  # event + event + decision (decision is event-typed)
    assert signals["event_span_days"] == 19  # 2026-04-01 to 2026-04-20


def test_compute_signals_buckets_media_by_type() -> None:
    media = [
        {"kind": "image", "url": "screenshot.png", "source_fact_id": "f1"},
        {"kind": "image", "url": "graph.png"},  # no source_fact_id → gallery
        {"url": "https://youtube.com/watch?v=x"},
        {"url": "https://example.com/doc.pdf"},
        {"url": "https://example.com/article"},  # generic link
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": []}, media=media)
    assert signals["inline_media_count"] == 1
    assert signals["gallery_media_count"] == 1
    assert signals["video_media_count"] == 1
    assert signals["pdf_media_count"] == 1
    assert signals["link_media_count"] == 1


def test_compute_signals_detects_hero_candidate() -> None:
    media = [
        {
            "kind": "image",
            "url": "dashboard.png",
            "alt": "Insights Dashboard",
            "referencing_fact_count": 4,
        }
    ]
    signals = compute_signals(
        cluster={"title": "Insights Dashboard", "member_facts": []}, media=media
    )
    assert signals["has_media_hero_candidate"] is True


def test_compute_signals_strong_claim_authors_distinct() -> None:
    facts = [
        {"fact_type": "opinion", "author_name": "Jacky"},
        {"fact_type": "opinion", "author_name": "Jacky"},
        {"fact_type": "claim", "author_name": "Thomas"},
        {"fact_type": "decision", "author_name": "Alan"},
        {"fact_type": "event", "author_name": "Pete"},  # not strong-claim
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": facts})
    assert signals["strong_claim_author_count"] == 3  # Jacky, Thomas, Alan


def test_compute_signals_handles_missing_dates_gracefully() -> None:
    cluster = {
        "title": "T",
        "member_facts": [
            {"fact_type": "event"},
            {"fact_type": "event", "date": "bogus"},
        ],
    }
    signals = compute_signals(cluster=cluster)
    assert signals["event_span_days"] == 0  # no parsable dates


# ---------------------------------------------------------------------------
# _validate_plan — drops invalid picks, dedups anchors, accepts media pins
# ---------------------------------------------------------------------------


def test_validate_plan_drops_unknown_module_ids() -> None:
    raw = {"modules": [{"id": "key_facts", "anchor": "kf"}, {"id": "bogus_module"}]}
    signals = {"fact_count": 10}
    plan = _validate_plan(raw, signals)
    ids = [m["id"] for m in plan.modules]
    assert ids == ["key_facts"]


def test_validate_plan_drops_modules_failing_eligibility() -> None:
    raw = {
        "modules": [
            {"id": "key_facts", "anchor": "kf"},  # needs fact_count ≥ 5
            {"id": "comparison_matrix", "anchor": "cm"},  # needs alternative_count ≥ 2
        ]
    }
    signals = {"fact_count": 10, "alternative_count": 0}
    plan = _validate_plan(raw, signals)
    ids = [m["id"] for m in plan.modules]
    assert "key_facts" in ids
    assert "comparison_matrix" not in ids


def test_validate_plan_dedups_anchors() -> None:
    raw = {
        "modules": [
            {"id": "key_facts", "anchor": "x"},
            {"id": "decision_log", "anchor": "x"},
        ]
    }
    signals = {"fact_count": 10, "decision_count": 3}
    plan = _validate_plan(raw, signals)
    anchors = [m["anchor"] for m in plan.modules]
    assert anchors == ["x", "x-2"]


def test_validate_plan_keeps_valid_media_pins() -> None:
    raw = {
        "modules": [{"id": "key_facts"}],
        "media_pins": [
            {"media_id": "m1", "fact_id": "f1", "slot": "inline"},
            {"media_id": "m2", "fact_id": "f2", "slot": "ghost_slot"},  # invalid
        ],
    }
    signals = {"fact_count": 10}
    plan = _validate_plan(raw, signals)
    assert len(plan.media_pins) == 1
    assert plan.media_pins[0].slot == "inline"


def test_validate_plan_handles_garbage_modules_field() -> None:
    raw = {"modules": "not a list"}
    plan = _validate_plan(raw, {"fact_count": 1})
    assert plan.modules == []


# ---------------------------------------------------------------------------
# ModulePlan dataclass
# ---------------------------------------------------------------------------


def test_module_plan_to_dict_round_trip() -> None:
    plan = ModulePlan(
        modules=[{"id": "key_facts", "anchor": "kf"}],
    )
    d = plan.to_dict()
    assert d["modules"] == [{"id": "key_facts", "anchor": "kf"}]
    assert d["media_pins"] == []


def test_module_plan_is_empty_predicate() -> None:
    assert ModulePlan().is_empty() is True
    assert ModulePlan(modules=[{"id": "x"}]).is_empty() is False
