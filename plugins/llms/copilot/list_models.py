"""List models available from the GitHub Copilot API.

Usage:
    python -m plugins.llms.copilot.list_models
    # or from project root:
    uv run python -m plugins.llms.copilot.list_models

Authentication (first match wins):
    COPILOT_GITHUB_TOKEN=...   # explicit Copilot token
    GH_TOKEN=...               # GitHub CLI compatible
    GITHUB_TOKEN=...           # fallback PAT
    gh auth token              # auto-detected from GitHub CLI
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    from plugins.llms.copilot._llm_patch import get_copilot_token, COPILOT_API_BASE
    import urllib.request

    token = get_copilot_token()
    if not token:
        print(
            "ERROR: No GitHub token found.\n"
            "  Set COPILOT_GITHUB_TOKEN, GH_TOKEN, or run: gh auth login",
            file=sys.stderr,
        )
        sys.exit(1)

    req = urllib.request.Request(
        f"{COPILOT_API_BASE}/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"ERROR: Failed to fetch models: {exc}", file=sys.stderr)
        sys.exit(1)

    models = data.get("data", [])
    print(f"GitHub Copilot API — {len(models)} models available\n")
    print(f"{'ID':<45} {'Vendor':<15} {'Version'}")
    print("-" * 75)
    for m in sorted(models, key=lambda x: x.get("id", "")):
        vendor = m.get("vendor", "")
        version = m.get("version", "")
        print(f"{m['id']:<45} {vendor:<15} {version}")

    print(
        "\nTo use a model in beever-atlas, set in .env:\n"
        "  LLM_FAST_MODEL=copilot/<model-id>\n"
        "  LLM_QUALITY_MODEL=copilot/<model-id>"
    )


if __name__ == "__main__":
    main()
