"""Tiny mermaid helpers shared by flow_chart + entity_diagram modules.

Mermaid is picky about ID syntax (no spaces, no special chars except
underscore + alphanumeric) and label content (parentheses, pipes,
brackets, quotes break the parser). Centralising the sanitizer here
keeps both modules emitting renderable diagrams.
"""

from __future__ import annotations

import re

_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
# Characters that break mermaid label parsing inside [] or () nodes.
_LABEL_FORBIDDEN = ("[", "]", "(", ")", "|", '"', "`", "#", ";", "\n", "\r")


def safe_id(raw: str, fallback: str = "N") -> str:
    """Squash a string into a mermaid-safe ID. Empty input returns
    the fallback so callers can iterate counter-suffixed IDs without
    branching on the empty case."""
    if not raw:
        return fallback
    out = _ID_SAFE_RE.sub("_", raw).strip("_")
    return out or fallback


def safe_label(raw: str, max_len: int = 60) -> str:
    """Strip characters mermaid mis-parses inside `[label]`. Truncate
    long labels with an ellipsis since mermaid wraps awkwardly."""
    if not raw:
        return ""
    out = raw
    for ch in _LABEL_FORBIDDEN:
        out = out.replace(ch, " ")
    out = " ".join(out.split())
    if len(out) > max_len:
        out = out[: max_len - 1].rstrip() + "…"
    return out
