"""``key_facts`` module — wraps the existing ``render_key_facts_table``.

Existed before this change as a deterministic compiler block; the
adaptive-modules system normalises it into a module so the catalog has
a single entry point and the planner can pick it like any other module.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.render import render_key_facts_table


def render(data: dict[str, Any]) -> str:
    """Render a Key Facts GFM table.

    ``data`` must contain ``facts: list[dict]`` (each fact at minimum
    ``memory_text``/``fact_type``/``importance``). Empty list returns
    an empty string — caller decides whether to skip the module.
    """
    facts = data.get("facts") or []
    if not isinstance(facts, list):
        return ""
    max_rows = int(data.get("max_rows", 8))
    return render_key_facts_table(facts, max_rows=max_rows)
