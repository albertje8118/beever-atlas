"""Fetch ChatGPT conversation history via CDP (uses your authenticated Edge session).

Requires Edge to be running with remote debugging enabled and an authenticated
chatgpt.com tab open.  See the plugin README for setup instructions.

Saves to ``chatgpt_history.json`` in the project root.

Usage::

    # From the project root:
    python -m plugins.chatgpt_copilot.chatgpt.fetch

    # Or via the convenience wrapper at project root:
    python fetch_chatgpt.py
"""

import websocket
import json
import time
import urllib.request
import pathlib
import sys

# Project root is 3 levels above this file: plugins/chatgpt_copilot/chatgpt/fetch.py
_PROJECT_ROOT = pathlib.Path(__file__).parents[3]
TOKEN_FILE = _PROJECT_ROOT / "chatgpt_token.txt"
OUTPUT_FILE = _PROJECT_ROOT / "chatgpt_history.json"
CDP_URL = "http://localhost:9222"


def get_chatgpt_tab():
    pages = json.loads(urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5).read())
    return next(
        (p for p in pages if "chatgpt.com" in p.get("url", "") and p.get("type") == "page"),
        None,
    )


def cdp_eval(ws, expr, msg_id, await_promise=False, timeout=60):
    ws.send(json.dumps({
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "awaitPromise": await_promise},
    }))
    ws.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                result = msg.get("result", {}).get("result", {})
                exc = msg.get("result", {}).get("exceptionDetails")
                if exc:
                    print(
                        f"  [CDP exception] {exc.get('text')}: "
                        f"{exc.get('exception', {}).get('description', '')[:200]}"
                    )
                    return None
                if result.get("type") == "string":
                    return result.get("value")
                return None
        except websocket.WebSocketTimeoutException:
            break
    return None


def walk_messages(mapping, current_node):
    """Walk the ChatGPT conversation tree from *current_node* to root."""
    messages = []
    node_id = current_node
    visited = set()
    while node_id and node_id not in visited:
        visited.add(node_id)
        node = mapping.get(node_id, {})
        msg = node.get("message")
        if msg and msg.get("content", {}).get("parts"):
            role = msg.get("author", {}).get("role", "?")
            parts = msg["content"]["parts"]
            text = " ".join(p for p in parts if isinstance(p, str)).strip()
            if text and role != "system":
                messages.insert(0, {"role": role, "text": text})
        node_id = node.get("parent")
    return messages


def main():
    if not TOKEN_FILE.exists():
        print(
            f"ERROR: Token file not found at {TOKEN_FILE}.\n"
            "Extract it via CDP:\n"
            '  fetch("/api/auth/session").then(r=>r.json()).then(d=>console.log(d.accessToken))'
        )
        sys.exit(1)

    token = TOKEN_FILE.read_text().strip()

    tab = get_chatgpt_tab()
    if not tab:
        print(
            "ERROR: No chatgpt.com tab found in Edge.\n"
            "Open Edge with --remote-debugging-port=9222 and navigate to chatgpt.com."
        )
        sys.exit(1)

    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], suppress_origin=True)
    print(f"Connected to: {tab['url']}")

    def cdp_fetch_json(url, msg_id, timeout=15):
        """Fetch a chatgpt.com backend URL via in-browser fetch() using CDP."""
        js = (
            'fetch("URL", {headers: {"Authorization": "Bearer TOKEN"}})'
            ".then(r=>r.json()).then(d=>JSON.stringify(d))"
            .replace("URL", url)
            .replace("TOKEN", token)
        )
        raw = cdp_eval(ws, js, msg_id=msg_id, await_promise=True, timeout=timeout)
        return json.loads(raw) if raw else None

    # Step 1: Fetch all conversation stubs (paginated, active + archived)
    print("Fetching conversation list...")
    stubs = []
    for archived in [False, True]:
        offset = 0
        while True:
            url = (
                f"/backend-api/conversations?offset={offset}&limit=28&order=updated"
                + ("&is_archived=true" if archived else "")
            )
            data = cdp_fetch_json(url, msg_id=len(stubs) + 200)
            if not data or not data.get("items"):
                break
            for item in data["items"]:
                item["_archived"] = archived
            stubs.extend(data["items"])
            if len([s for s in stubs if s["_archived"] == archived]) >= data.get("total", 0):
                break
            offset += 28

    active = sum(1 for s in stubs if not s["_archived"])
    archived_count = sum(1 for s in stubs if s["_archived"])
    print(f"Found {len(stubs)} conversations ({active} active, {archived_count} archived).")

    # Step 2: Fetch full content for each conversation
    all_convs = []
    for i, stub in enumerate(stubs):
        data = cdp_fetch_json(f"/backend-api/conversation/{stub['id']}", msg_id=1000 + i)
        messages = walk_messages(data.get("mapping", {}), data.get("current_node")) if data else []

        all_convs.append({
            "id": stub["id"],
            "title": stub["title"],
            "created": stub.get("create_time"),
            "updated": stub.get("update_time"),
            "archived": stub.get("_archived", False),
            "messages": messages,
        })
        if (i + 1) % 10 == 0 or i == len(stubs) - 1:
            print(f"  [{i+1}/{len(stubs)}] fetched '{stub['title'][:50]}'")

    ws.close()

    OUTPUT_FILE.write_text(json.dumps(all_convs, indent=2, ensure_ascii=False), encoding="utf-8")
    total_msgs = sum(len(c["messages"]) for c in all_convs)
    print(f"\nSaved {len(all_convs)} conversations / {total_msgs} messages → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
