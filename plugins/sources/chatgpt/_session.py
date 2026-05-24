"""Browser-session helpers for ChatGPT source authentication.

Authentication approach
-----------------------
1.  Read cookies:  Reads chatgpt.com cookies directly from the user's
    installed Edge or Chrome profile on disk.  We copy the SQLite cookie
    database to a temp file (no shadow copy / no admin required) and decrypt
    using the DPAPI-protected AES key stored in ``Local State``.

2.  Fetch (every sync):  ``httpx.AsyncClient`` uses those cookies to call
    ChatGPT's backend APIs directly — no browser needs to be open.

3.  Re-auth:  When the session expires the backend reports
    ``"authenticated": false``.  The UI shows an "Open ChatGPT" button that
    opens chatgpt.com in the user's default browser so they can log in again.

No CDP port, no Playwright, no shadow copy, no admin privileges required.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import webbrowser
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from plugins.sources.chatgpt.fetch import walk_messages

logger = logging.getLogger(__name__)

_CHATGPT_ORIGIN = "https://chatgpt.com"
_CONV_LIMIT = 100
_PROJECT_ROOT = pathlib.Path(__file__).parents[3]
_TOKEN_FILE = _PROJECT_ROOT / "chatgpt_token.txt"

# Progress tracker — updated during history fetch so the API can stream counts
_fetch_progress: dict[str, Any] = {"fetched": 0, "running": False}

# ---------------------------------------------------------------------------
# Token-file helpers  (chatgpt_token.txt — manual / bookmarklet import)
# ---------------------------------------------------------------------------

def _load_saved_token() -> str | None:
    """Return the access token from chatgpt_token.txt, or None if missing/expired."""
    if not _TOKEN_FILE.exists():
        return None
    token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        return None
    # Decode expiry from the JWT payload (no signature verification needed here)
    parts = token.split(".")
    if len(parts) != 3:
        logger.warning("chatgpt session: token file has invalid JWT format, ignoring")
        return None
    try:
        payload_b64 = parts[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        payload_b64 += "=" * (padding % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp", 0)
        if exp and exp < time.time():
            logger.info("chatgpt session: saved token expired at %s", exp)
            return None
    except Exception:  # noqa: BLE001
        logger.warning("chatgpt session: token file payload could not be decoded, ignoring")
        return None
    return token


def save_token(token: str) -> None:
    """Write an access token to chatgpt_token.txt."""
    _TOKEN_FILE.write_text(token.strip(), encoding="utf-8")
    logger.info("chatgpt session: token saved to %s", _TOKEN_FILE)

# ---------------------------------------------------------------------------
# Windows cookie decryption helpers (Edge / Chrome, no admin needed)
# ---------------------------------------------------------------------------

def _dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt DPAPI-protected bytes using win32crypt (pywin32) or ctypes."""
    try:
        import win32crypt  # type: ignore[import-untyped]  # pywin32
        return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]
    except ImportError:
        pass
    # ctypes fallback
    import ctypes
    import ctypes.wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(ctypes.sizeof(buf), buf)
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _chromium_aes_key(user_data_dir: str) -> bytes | None:
    """Extract and DPAPI-decrypt the AES-256-GCM key from a Chromium Local State file."""
    state_path = os.path.join(user_data_dir, "Local State")
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        b64 = state.get("os_crypt", {}).get("encrypted_key", "")
        if not b64:
            return None
        raw = base64.b64decode(b64)
        if raw[:5] != b"DPAPI":
            return None
        return _dpapi_decrypt(raw[5:])
    except Exception as exc:  # noqa: BLE001
        logger.debug("chatgpt session: failed to read AES key from %s: %s", user_data_dir, exc)
        return None


def _decrypt_chromium_value(key: bytes, enc: bytes) -> str:
    """Decrypt a Chromium v10/v11 AES-GCM encrypted cookie value."""
    if not enc:
        return ""
    prefix = enc[:3]
    if prefix in (b"v10", b"v11"):
        try:
            # Format: 3-byte prefix | 12-byte nonce | ciphertext+tag
            return AESGCM(key).decrypt(enc[3:15], enc[15:], None).decode("utf-8", errors="replace")
        except Exception:
            return ""
    # Legacy per-cookie DPAPI (older profiles)
    try:
        return _dpapi_decrypt(enc).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _read_chromium_profile_cookies(user_data_dir: str, domain_substr: str) -> dict[str, str]:
    """Copy Chromium's cookie DB to a temp file and read matching cookies.

    Using a plain ``shutil.copy2`` avoids the admin-requiring Shadow Copy that
    ``browser_cookie3`` uses.  Python opens files with FILE_SHARE_READ on
    Windows, so this works even while the browser is running.

    Scans Default profile first, then Profile 1, Profile 2, … stopping at the
    first profile that has cookies for the given domain.
    """
    if not os.path.isdir(user_data_dir):
        return {}

    key = _chromium_aes_key(user_data_dir)
    if key is None:
        return {}

    # Enumerate profiles: Default first, then numbered ones
    profile_dirs = ["Default"]
    try:
        for entry in sorted(os.listdir(user_data_dir)):
            if entry.startswith("Profile") and os.path.isdir(os.path.join(user_data_dir, entry)):
                profile_dirs.append(entry)
    except OSError:
        pass

    for profile in profile_dirs:
        for sub in ("Network/Cookies", "Cookies"):
            cookie_file = os.path.join(user_data_dir, profile, *sub.split("/"))
            if not os.path.exists(cookie_file):
                continue

            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(tmp_fd)
            try:
                shutil.copy2(cookie_file, tmp_path)
                con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
                        (f"%{domain_substr}%",),
                    ).fetchall()
                finally:
                    con.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("chatgpt session: DB read error %s/%s: %s", profile, sub, exc)
                continue
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            result: dict[str, str] = {}
            for name, enc_val in rows:
                value = _decrypt_chromium_value(key, enc_val)
                if value:
                    result[name] = value

            if result:
                logger.debug(
                    "chatgpt session: found %d cookies in %s/%s",
                    len(result), profile, sub,
                )
                return result

    return {}


# ---------------------------------------------------------------------------
# Cookie reader — tries Edge, Chrome, then browser_cookie3 for others
# ---------------------------------------------------------------------------

def _read_browser_cookies() -> dict[str, str] | None:
    """Return chatgpt.com cookies from the user's installed browser.

    Primary: direct Edge/Chrome reading (no admin, no shadow copy).
    Fallback: browser_cookie3 for Firefox and other browsers.
    """
    local = os.environ.get("LOCALAPPDATA", "") if sys.platform == "win32" else ""

    # --- Edge (primary, works without admin) ---
    if local:
        edge_dir = os.path.join(local, "Microsoft", "Edge", "User Data")
        cookies = _read_chromium_profile_cookies(edge_dir, "chatgpt.com")
        if cookies:
            logger.debug("chatgpt session: read %d cookies from Edge (direct)", len(cookies))
            return cookies

    # --- Chrome direct (works on Chrome < 127; Chrome 127+ uses App-Bound encryption) ---
    if local:
        chrome_dir = os.path.join(local, "Google", "Chrome", "User Data")
        cookies = _read_chromium_profile_cookies(chrome_dir, "chatgpt.com")
        if cookies:
            logger.debug("chatgpt session: read %d cookies from Chrome (direct)", len(cookies))
            return cookies

    # --- Fallback: browser_cookie3 for Firefox / Brave / Opera / etc. ---
    try:
        import browser_cookie3  # type: ignore[import-untyped]

        for name, label in [("firefox", "Firefox"), ("brave", "Brave"), ("opera", "Opera")]:
            getter = getattr(browser_cookie3, name, None)
            if getter is None:
                continue
            try:
                jar = getter(domain_name="chatgpt.com")
                cookies = {c.name: c.value for c in jar if c.value}
                if cookies:
                    logger.debug("chatgpt session: read %d cookies from %s", len(cookies), label)
                    return cookies
            except Exception as exc:  # noqa: BLE001
                logger.debug("chatgpt session: %s failed: %s", label, exc)
    except ImportError:
        pass

    return None


# ---------------------------------------------------------------------------
# Session probe (httpx — no browser launch)
# ---------------------------------------------------------------------------

async def _get_access_token(cookies: dict[str, str]) -> str | None:
    """Call /api/auth/session with the provided cookies; return the access token."""
    try:
        async with httpx.AsyncClient(
            base_url=_CHATGPT_ORIGIN,
            cookies=cookies,
            follow_redirects=True,
            timeout=15,
        ) as client:
            resp = await client.get("/api/auth/session")
            if resp.status_code == 200:
                return resp.json().get("accessToken")
    except httpx.HTTPError:
        pass
    return None


async def probe_browser_session() -> dict[str, Any]:
    """Return session availability by checking token file then browser cookies."""
    # 1. Token file (fastest — no I/O to browser DBs)
    token = _load_saved_token()
    if token:
        return {"browser_available": True, "authenticated": True, "reason": None, "source": "token_file"}

    # 2. Browser cookies (Edge/Chrome direct read)
    cookies = _read_browser_cookies()
    if not cookies:
        return {
            "browser_available": False,
            "authenticated": False,
            "reason": "no_token_file_and_no_browser_cookies",
        }
    api_token = await _get_access_token(cookies)
    if api_token:
        return {"browser_available": True, "authenticated": True, "reason": None, "source": "browser_cookies"}
    return {
        "browser_available": True,
        "authenticated": False,
        "reason": "session_expired_or_not_logged_in",
    }


# ---------------------------------------------------------------------------
# "Launch browser" — open chatgpt.com in the user's default browser
# ---------------------------------------------------------------------------

def get_auth_status() -> dict[str, Any]:
    """Return a static status dict (no background task needed)."""
    return {"status": "idle", "note": "open chatgpt.com to log in, then probe session"}


async def launch_auth_browser() -> dict[str, str]:
    """Open the Beever Atlas ChatGPT setup wizard in the user's default browser.

    The setup page walks non-technical users through the 3-step token import
    process without requiring any developer tools or bookmarklets.
    """
    port = int(os.environ.get("PORT", os.environ.get("BEEVER_PORT", "8000")))
    setup_url = f"http://localhost:{port}/api/plugins/chatgpt/setup"
    try:
        webbrowser.open(setup_url)
        return {"status": "browser_opened", "url": setup_url}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": str(exc)}


# Backward-compat alias
async def launch_debug_browser() -> dict[str, str]:
    return await launch_auth_browser()


# ---------------------------------------------------------------------------
# History fetch (httpx — no browser)
# ---------------------------------------------------------------------------

async def _fetch_conv_pages(
    client: httpx.AsyncClient,
    base_url: str,
    archived: bool,
) -> list[dict[str, Any]]:
    results: list[dict] = []
    next_cursor: str | None = None
    offset = 0
    while True:
        sep = "&" if "?" in base_url else "?"
        if next_cursor:
            # Cursor-based pagination (newer ChatGPT API)
            url = f"{base_url}{sep}cursor={next_cursor}&limit={_CONV_LIMIT}"
        else:
            url = f"{base_url}{sep}offset={offset}&limit={_CONV_LIMIT}"
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            break
        if resp.status_code != 200:
            break
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001  # non-JSON response (e.g. redirect to login HTML)
            break
        items: list[dict] = data.get("items") or []
        if not items:
            break
        for item in items:
            item["_archived"] = archived
        results.extend(items)
        _fetch_progress["fetched"] = (_fetch_progress.get("fetched") or 0) + len(items)
        # Prefer cursor if the API provides one (more reliable than offset for large accounts)
        next_cursor = data.get("cursor") or None
        if len(items) < _CONV_LIMIT:
            break  # Last page — no more to fetch
        if not next_cursor:
            offset += _CONV_LIMIT
    return results


def get_fetch_progress() -> dict[str, Any]:
    """Return a snapshot of the current fetch progress."""
    return dict(_fetch_progress)


async def fetch_all_stubs() -> list[dict[str, Any]]:
    """Fetch all conversation stubs (title + metadata, no messages).

    Much faster than export_history_from_browser() — does NOT fetch individual
    conversation content.  Use this to refresh the channel-picker cache.
    """
    global _fetch_progress
    _fetch_progress = {"fetched": 0, "running": True}
    try:
        return await _do_fetch_stubs()
    finally:
        _fetch_progress["running"] = False


async def _do_fetch_stubs() -> list[dict[str, Any]]:
    """Internal stub-fetch implementation shared by fetch_all_stubs."""
    token = _load_saved_token()
    cookies: dict[str, str] = {}
    if not token:
        cookies = _read_browser_cookies() or {}
        if not cookies:
            raise RuntimeError(
                "No saved token and no browser cookies found. "
                "Use the token import wizard in Settings to connect."
            )
        token = await _get_access_token(cookies)
        if not token:
            raise RuntimeError(
                "ChatGPT session has expired or you are not logged in. "
                "Re-import your session token in Settings."
            )

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    async with httpx.AsyncClient(
        base_url=_CHATGPT_ORIGIN,
        cookies=cookies,
        headers=headers,
        follow_redirects=True,
        timeout=30,
    ) as client:
        stubs: list[dict] = []
        stubs.extend(await _fetch_conv_pages(client, "/backend-api/conversations", archived=False))
        stubs.extend(await _fetch_conv_pages(client, "/backend-api/conversations?is_archived=true", archived=True))

        # Projects
        try:
            proj_resp = await client.get("/backend-api/projects?offset=0&limit=50")
            if proj_resp.status_code == 200:
                proj_data = proj_resp.json()
                projects = proj_data.get("projects") or proj_data.get("items") or []
                for proj in projects:
                    proj_id = proj.get("id")
                    proj_name = proj.get("name") or proj_id
                    if not proj_id:
                        continue
                    proj_stubs = await _fetch_conv_pages(
                        client,
                        f"/backend-api/projects/{proj_id}/conversations",
                        archived=False,
                    )
                    for s in proj_stubs:
                        s["_project_id"] = proj_id
                        s["_project_name"] = proj_name
                    stubs.extend(proj_stubs)
        except httpx.HTTPError:
            pass

        # Deduplicate by conversation id
        seen: set[str] = set()
        unique: list[dict] = []
        for s in stubs:
            cid = s.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(s)
        return unique


async def export_history_from_browser() -> list[dict[str, Any]]:
    """Fetch ChatGPT history using a saved token or browser cookies.

    Priority:
    1. ``chatgpt_token.txt`` — saved via the bookmarklet import route
    2. Browser cookie databases (Edge/Chrome direct read, no admin)

    Raises ``RuntimeError`` when no usable auth is available.
    """
    global _fetch_progress
    _fetch_progress = {"fetched": 0, "running": True}
    try:
        return await _do_export_history()
    finally:
        _fetch_progress["running"] = False


async def _do_export_history() -> list[dict[str, Any]]:
    """Internal implementation — called by export_history_from_browser()."""
    # --- 1. Token file ---
    token = _load_saved_token()
    cookies: dict[str, str] = {}

    if not token:
        # --- 2. Browser cookies ---
        cookies = _read_browser_cookies() or {}
        if not cookies:
            raise RuntimeError(
                "No saved token and no browser cookies found. "
                "Use the 'Get Token' bookmarklet on chatgpt.com to import your session."
            )
        token = await _get_access_token(cookies)
        if not token:
            raise RuntimeError(
                "ChatGPT session has expired or you are not logged in. "
                "Use the 'Get Token' bookmarklet on chatgpt.com to import your session."
            )

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    async with httpx.AsyncClient(
        base_url=_CHATGPT_ORIGIN,
        cookies=cookies,
        headers=headers,
        follow_redirects=True,
        timeout=30,
    ) as client:
        stubs: list[dict] = []
        stubs.extend(
            await _fetch_conv_pages(client, "/backend-api/conversations", archived=False)
        )
        stubs.extend(
            await _fetch_conv_pages(
                client, "/backend-api/conversations?is_archived=true", archived=True
            )
        )

        # Projects
        try:
            proj_resp = await client.get("/backend-api/projects?offset=0&limit=50")
            if proj_resp.status_code == 200:
                proj_data = proj_resp.json()
                projects = proj_data.get("projects") or proj_data.get("items") or []
                for proj in projects:
                    proj_id = proj.get("id")
                    proj_name = proj.get("name") or proj_id
                    if not proj_id:
                        continue
                    proj_stubs = await _fetch_conv_pages(
                        client,
                        f"/backend-api/projects/{proj_id}/conversations",
                        archived=False,
                    )
                    for s in proj_stubs:
                        s["_project_id"] = proj_id
                        s["_project_name"] = proj_name
                    stubs.extend(proj_stubs)
        except httpx.HTTPError:
            pass

        # Deduplicate by conversation id
        seen: set[str] = set()
        unique: list[dict] = []
        for s in stubs:
            cid = s.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(s)

        conversations: list[dict] = []
        for stub in unique:
            try:
                resp = await client.get(f"/backend-api/conversation/{stub['id']}")
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            data = resp.json()
            messages = walk_messages(data.get("mapping", {}), data.get("current_node"))
            conversations.append(
                {
                    "id": stub["id"],
                    "title": stub.get("title") or "Untitled ChatGPT conversation",
                    "created": stub.get("create_time"),
                    "updated": stub.get("update_time"),
                    "archived": stub.get("_archived", False),
                    "pinned": bool(stub.get("is_pinned")),
                    "project_id": stub.get("_project_id"),
                    "project_name": stub.get("_project_name"),
                    "messages": messages,
                }
            )

        return conversations


async def fetch_conversations_by_ids(
    conversation_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch full message content for a specific list of conversation IDs.

    Called during ingestion (Save Conversations / Refresh Now) so that only the
    selected conversations pay the per-request cost, not the entire history.

    Conversations that can't be fetched (deleted, 404, network error) are
    silently skipped.
    """
    if not conversation_ids:
        return []

    token = _load_saved_token()
    cookies: dict[str, str] = {}
    if not token:
        cookies = _read_browser_cookies() or {}
        if not cookies:
            raise RuntimeError(
                "No saved token and no browser cookies found. "
                "Re-import your session token in Settings."
            )
        token = await _get_access_token(cookies)
        if not token:
            raise RuntimeError(
                "ChatGPT session has expired or you are not logged in. "
                "Re-import your session token in Settings."
            )

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    async with httpx.AsyncClient(
        base_url=_CHATGPT_ORIGIN,
        cookies=cookies,
        headers=headers,
        follow_redirects=True,
        timeout=30,
    ) as client:
        results: list[dict] = []
        for conv_id in conversation_ids:
            try:
                resp = await client.get(f"/backend-api/conversation/{conv_id}")
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                continue
            messages = walk_messages(data.get("mapping", {}), data.get("current_node"))
            results.append(
                {
                    "id": conv_id,
                    "title": data.get("title") or "Untitled ChatGPT conversation",
                    "created": data.get("create_time"),
                    "updated": data.get("update_time"),
                    "archived": bool(data.get("is_archived")),
                    "pinned": bool(data.get("is_pinned")),
                    "project_id": (data.get("conversation_template_id") or None),
                    "project_name": None,
                    "messages": messages,
                }
            )
        return results
