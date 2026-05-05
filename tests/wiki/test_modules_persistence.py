"""Round-trip tests for the new ``modules`` field on WikiPage.

Verifies:
- Legacy rows (without a ``modules`` field) deserialize as ``modules: []``
- New rows preserve their modules list across persistence round-trips
- Domain WikiPage and persistence WikiPage both have the field with
  the same default
"""

from __future__ import annotations

from beever_atlas.models.domain import WikiPage as DomainWikiPage
from beever_atlas.models.persistence import WikiPage as PersistenceWikiPage


def test_persistence_wiki_page_modules_default_empty() -> None:
    """A WikiPage created without specifying ``modules`` defaults to
    an empty list — required for legacy-row backward compatibility."""
    page = PersistenceWikiPage(
        channel_id="C_TEST",
        page_id="topic-auth",
        slug="topic-auth",
        title="Authentication",
    )
    assert page.modules == []


def test_persistence_wiki_page_modules_round_trip() -> None:
    """A page persisted with modules round-trips through ``model_dump``
    and ``model_validate`` with module IDs and anchors preserved."""
    original = PersistenceWikiPage(
        channel_id="C_TEST",
        page_id="topic-auth",
        slug="topic-auth",
        title="Authentication",
        modules=[
            {"id": "key_facts", "anchor": "kf1"},
            {"id": "decision_log", "anchor": "dl1", "data": {"row_count": 3}},
        ],
    )
    dumped = original.model_dump()
    restored = PersistenceWikiPage.model_validate(dumped)
    assert restored.modules == original.modules
    assert restored.modules[1]["data"]["row_count"] == 3


def test_persistence_legacy_dict_deserializes_with_empty_modules() -> None:
    """Mongo docs persisted before this change have NO ``modules`` key.
    Pydantic must default to an empty list — never raise."""
    legacy_doc = {
        "channel_id": "C_TEST",
        "page_id": "topic-old",
        "slug": "topic-old",
        "title": "Old Topic",
        # No "modules" key — simulates pre-change row.
    }
    page = PersistenceWikiPage.model_validate(legacy_doc)
    assert page.modules == []


def test_domain_wiki_page_modules_default_empty() -> None:
    """The domain WikiPage (used by the API/serializer) shares the
    same default — round-trips on the API side stay consistent."""
    page = DomainWikiPage(id="topic-auth", slug="topic-auth", title="Authentication")
    assert page.modules == []


def test_domain_wiki_page_modules_round_trip() -> None:
    """Domain model round-trips its modules list intact."""
    original = DomainWikiPage(
        id="topic-auth",
        slug="topic-auth",
        title="Authentication",
        modules=[{"id": "open_questions", "anchor": "oq1"}],
    )
    dumped = original.model_dump()
    restored = DomainWikiPage.model_validate(dumped)
    assert restored.modules == original.modules
