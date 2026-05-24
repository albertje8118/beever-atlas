type ChatGPTConnection = {
  id: string;
  platform: string;
  display_name: string;
  selected_channels: string[];
  status: "connected" | "disconnected" | "error";
  error_message: string | null;
  source: string;
  created_at: string;
  updated_at: string;
};

type ChatGPTStatus = {
  history_file_exists: boolean;
  total_conversations: number;
  connected_sources: number;
  selected_conversations: number;
  browser_available: boolean;
  browser_authenticated: boolean;
  browser_reason?: string | null;
  auth_source?: "token_file" | "browser_cookies" | null;
  sync_interval_hours?: number;
};

type ChatGPTChannel = {
  channel_id: string;
  name: string;
  topic?: string | null;
  member_count?: number | null;
  is_member?: boolean;
};

type WizardState = {
  mode: "create" | "manage";
  connection: ChatGPTConnection | null;
  displayName: string;
  authMode: "browser" | "file_only";
  channels: ChatGPTChannel[];
  selected: Set<string>;
  loading: boolean;
  fetching: boolean;
  fetchResult: string | null;
  saving: boolean;
  error: string | null;
  step: "setup" | "token-import" | "channels";
  tokenSaving: boolean;
};

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
const API_KEY = import.meta.env.VITE_BEEVER_API_KEY as string | undefined;
const OFFICIAL_CHATGPT_ICON_URL = "https://chatgpt.com/favicon.ico";
const STYLE_ID = "beever-chatgpt-overlay-style";
const SETTINGS_SECTION_ID = "beever-chatgpt-plugin-section";
const WELCOME_TILE_ID = "beever-chatgpt-plugin-welcome-tile";
const PICKER_TILE_ID = "beever-chatgpt-plugin-picker-tile";
const HEADER_BUTTON_ID = "beever-chatgpt-plugin-header-button";
const MODAL_ROOT_ID = "beever-chatgpt-plugin-modal";

let cachedConnections: ChatGPTConnection[] = [];
let cachedStatus: ChatGPTStatus | null = null;
let refreshScheduled = false;
const fetchingConnections = new Set<string>();
const fetchResults = new Map<string, string>();
let _progressTimer: ReturnType<typeof setInterval> | null = null;

function _startProgressPoll() {
  if (_progressTimer) return;
  _progressTimer = setInterval(async () => {
    try {
      const p = await apiGet<{ fetched: number; running: boolean }>("/api/plugins/chatgpt/fetch-progress");
      const label = p.fetched > 0 ? `${p.fetched} found` : "";
      document.querySelectorAll<HTMLElement>("[data-fetch-progress]").forEach((el) => { el.textContent = label; });
      const modalEl = document.getElementById("beever-chatgpt-modal-progress");
      if (modalEl) modalEl.textContent = label;
    } catch { /* ignore */ }
  }, 1500);
}

function _stopProgressPoll() {
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
}

function getAccessLabel(status: ChatGPTStatus | null): string {
  if (!status) return "Not connected";
  if (status.browser_authenticated) {
    if (status.auth_source === "token_file") return "Token file";
    if (status.auth_source === "browser_cookies") return "Live browser";
    return "Authenticated";
  }
  if (status.browser_available) return "Sign in needed";
  if (status.history_file_exists) return "Cached only";
  return "Not connected";
}

function authHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  if (API_KEY && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${API_KEY}`);
  }
  return headers;
}

async function parseError(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    return payload?.detail || payload?.message || payload?.error || response.statusText;
  } catch {
    return response.statusText || "Request failed";
  }
}

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: authHeaders(),
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json() as Promise<T>;
}

async function apiJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

function installStyles() {
  if (document.getElementById(STYLE_ID)) {
    return;
  }
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    .beever-chatgpt-card {
      border: 1px solid color-mix(in srgb, var(--border, #d9e2e7) 100%, transparent);
      border-radius: 1rem;
      background: color-mix(in srgb, var(--card, #ffffff) 94%, #10a37f 6%);
      padding: 1.25rem;
      margin: 0 0 1rem;
      box-shadow: 0 10px 30px rgba(16, 163, 127, 0.08);
    }
    .beever-chatgpt-card h3,
    .beever-chatgpt-card h4 {
      margin: 0;
      color: var(--foreground, #12242b);
    }
    .beever-chatgpt-card p,
    .beever-chatgpt-card span,
    .beever-chatgpt-card label {
      color: var(--muted-foreground, #5f7680);
    }
    .beever-chatgpt-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }
    .beever-chatgpt-meta {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 0.75rem;
      margin: 1rem 0;
    }
    .beever-chatgpt-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.25rem 0.65rem;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      background: rgba(16, 163, 127, 0.1);
      color: #0f8f70;
    }
    .beever-chatgpt-stat {
      border: 1px solid rgba(16, 163, 127, 0.12);
      border-radius: 0.9rem;
      background: rgba(16, 163, 127, 0.05);
      padding: 0.8rem 0.9rem;
    }
    .beever-chatgpt-stat strong {
      display: block;
      font-size: 1rem;
      color: var(--foreground, #12242b);
      margin-top: 0.2rem;
    }
    .beever-chatgpt-actions {
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-top: 1rem;
    }
    .beever-chatgpt-button,
    .beever-chatgpt-option {
      border: 1px solid rgba(16, 163, 127, 0.18);
      border-radius: 0.85rem;
      padding: 0.7rem 1rem;
      font: inherit;
      cursor: pointer;
      background: rgba(16, 163, 127, 0.08);
      color: #0f8f70;
      transition: background 120ms ease, transform 120ms ease;
    }
    .beever-chatgpt-button:hover,
    .beever-chatgpt-option:hover {
      background: rgba(16, 163, 127, 0.14);
      transform: translateY(-1px);
    }
    .beever-chatgpt-button.primary {
      background: #10a37f;
      color: white;
      border-color: #10a37f;
    }
    .beever-chatgpt-button.ghost {
      background: transparent;
      color: var(--muted-foreground, #5f7680);
      border-color: var(--border, #d9e2e7);
    }
    .beever-chatgpt-option {
      width: 100%;
      text-align: left;
      display: flex;
      align-items: flex-start;
      gap: 0.9rem;
    }
    .beever-chatgpt-icon {
      width: 2.5rem;
      height: 2.5rem;
      border-radius: 0.85rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      background: rgba(16, 163, 127, 0.12);
      color: #0f8f70;
      font-size: 1.15rem;
      font-weight: 800;
    }
    .beever-chatgpt-icon img {
      width: 1.2rem;
      height: 1.2rem;
      object-fit: contain;
    }
    .beever-chatgpt-modal {
      position: fixed;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1rem;
      z-index: 1000;
    }
    .beever-chatgpt-modal-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(0, 0, 0, 0.45);
      backdrop-filter: blur(6px);
    }
    .beever-chatgpt-modal-panel {
      position: relative;
      width: min(760px, calc(100vw - 2rem));
      max-height: calc(100vh - 2rem);
      overflow: auto;
      border-radius: 1.25rem;
      background: var(--card, #fff);
      border: 1px solid var(--border, #d9e2e7);
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.2);
      padding: 1.25rem;
    }
    .beever-chatgpt-modal-panel input[type="text"] {
      width: 100%;
      border: 1px solid var(--border, #d9e2e7);
      border-radius: 0.9rem;
      padding: 0.8rem 0.95rem;
      font: inherit;
      background: var(--background, #f8fbfc);
      color: var(--foreground, #12242b);
      margin-top: 0.45rem;
    }
    .beever-chatgpt-option-list {
      display: grid;
      gap: 0.75rem;
      margin-top: 1rem;
    }
    .beever-chatgpt-channel-list {
      display: grid;
      gap: 0.6rem;
      margin-top: 1rem;
      max-height: 360px;
      overflow: auto;
    }
    .beever-chatgpt-channel {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 0.75rem;
      align-items: flex-start;
      padding: 0.75rem 0.9rem;
      border-radius: 0.9rem;
      border: 1px solid rgba(16, 163, 127, 0.12);
      background: rgba(16, 163, 127, 0.04);
    }
    .beever-chatgpt-channel input {
      margin-top: 0.2rem;
    }
    .beever-chatgpt-error {
      margin-top: 1rem;
      border-radius: 0.85rem;
      background: rgba(190, 24, 93, 0.1);
      border: 1px solid rgba(190, 24, 93, 0.16);
      color: #b4235a;
      padding: 0.8rem 0.95rem;
      font-size: 0.92rem;
    }
    .beever-chatgpt-banner {
      margin-top: 0.85rem;
      border-radius: 0.75rem;
      padding: 0.75rem 0.95rem;
      font-size: 0.88rem;
      line-height: 1.5;
    }
    .beever-chatgpt-banner.info {
      background: rgba(59, 130, 246, 0.08);
      border: 1px solid rgba(59, 130, 246, 0.2);
      color: #1d4ed8;
    }
    .beever-chatgpt-banner.warn {
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.22);
      color: #92400e;
    }
    .beever-chatgpt-token-steps { display:grid; gap:1rem; margin-top:0.75rem; }
    .beever-chatgpt-token-step { display:flex; gap:1rem; align-items:flex-start; }
    .beever-chatgpt-token-num { background:#6366f1; color:#fff; border-radius:50%; min-width:2rem; height:2rem; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:0.85rem; flex-shrink:0; margin-top:0.1rem; }
    .beever-chatgpt-token-num.done { background:#16a34a; }
    .beever-chatgpt-manual-area { width:100%; border:1px solid var(--border,#e2e8f0); border-radius:0.6rem; padding:0.6rem; font-size:0.82rem; font-family:monospace; min-height:80px; resize:vertical; margin-top:0.6rem; background:var(--background,#f8fafc); color:var(--foreground,#12242b); }
    .beever-chatgpt-hidden-native {
      display: none !important;
    }
    @media (max-width: 700px) {
      .beever-chatgpt-meta {
        grid-template-columns: 1fr 1fr;
      }
      .beever-chatgpt-row {
        flex-direction: column;
      }
      .beever-chatgpt-modal-panel {
        padding: 1rem;
      }
    }
  `;
  document.head.append(style);
}

function chatgptIconMarkup() {
  return `<span class="beever-chatgpt-icon"><img src="${OFFICIAL_CHATGPT_ICON_URL}" alt="" /></span>`;
}

function scheduleRefresh() {
  if (refreshScheduled) {
    return;
  }
  refreshScheduled = true;
  window.setTimeout(() => {
    refreshScheduled = false;
    void refreshOverlay();
  }, 80);
}

async function refreshOverlay() {
  if (!isRelevantPage()) {
    removeTransientUI();
    return;
  }

  try {
    const [connections, status] = await Promise.all([
      apiGet<ChatGPTConnection[]>("/api/plugins/chatgpt/connections"),
      apiGet<ChatGPTStatus>("/api/plugins/chatgpt/status"),
    ]);
    cachedConnections = connections;
    cachedStatus = status;
  } catch {
    cachedConnections = [];
    cachedStatus = null;
  }

  // Inject UI once React has rendered the page content
  waitForPageContent();
}

function isRelevantPage() {
  return window.location.pathname.includes("/settings") || window.location.pathname === "/";
}

function removeTransientUI() {
  document.getElementById(SETTINGS_SECTION_ID)?.remove();
  document.getElementById(WELCOME_TILE_ID)?.remove();
  document.getElementById(PICKER_TILE_ID)?.remove();
  document.getElementById(HEADER_BUTTON_ID)?.remove();
}

function findElementByText<T extends Element>(selector: string, text: string): T | null {
  const elements = Array.from(document.querySelectorAll<T>(selector));
  return elements.find((element) => element.textContent?.trim() === text) ?? null;
}

function hideNativeFileCards() {
  for (const connection of cachedConnections) {
    const titles = Array.from(document.querySelectorAll("h3"));
    for (const title of titles) {
      if (title.textContent?.trim() !== connection.display_name) {
        continue;
      }
      const card = title.closest(".group");
      if (card && !card.classList.contains("beever-chatgpt-hidden-native")) {
        card.classList.add("beever-chatgpt-hidden-native");
      }
    }
  }
}

function injectSettingsHeaderButton() {
  if (!window.location.pathname.includes("/settings")) {
    return;
  }
  const addButton = Array.from(document.querySelectorAll("button")).find(
    (button) => button.textContent?.trim() === "Add Connection",
  );
  if (!addButton || document.getElementById(HEADER_BUTTON_ID)) {
    return;
  }
  const button = document.createElement("button");
  button.id = HEADER_BUTTON_ID;
  button.type = "button";
  button.className = "beever-chatgpt-button ghost";
  button.textContent = "ChatGPT History";
  button.addEventListener("click", () => openWizard(null));
  addButton.parentElement?.insertBefore(button, addButton);
}

function injectSettingsSection() {
  if (!window.location.pathname.includes("/settings")) {
    return;
  }
  const heading = findElementByText<HTMLHeadingElement>("h2", "Platform Connections");
  if (!heading) {
    document.getElementById(SETTINGS_SECTION_ID)?.remove();
    return;
  }
  const sectionHost = heading.parentElement;
  if (!sectionHost) {
    return;
  }
  let section = document.getElementById(SETTINGS_SECTION_ID);
  if (!section) {
    section = document.createElement("div");
    section.id = SETTINGS_SECTION_ID;
    heading.insertAdjacentElement("afterend", section);
  }

  const status = cachedStatus;

  const browserBannerMarkup = (connectionId: string | null): string => {
    if (status?.browser_authenticated) return "";
    const idAttr = connectionId ? ` data-connection-id="${connectionId}"` : "";
    return `<div class="beever-chatgpt-banner warn">
      ⚠ ChatGPT account not connected yet. Import your session token to enable history sync.
      <div style="display:flex;align-items:center;gap:0.5rem;margin-top:0.6rem;">
        <button class="beever-chatgpt-button" data-action="connect-account"${idAttr}>Connect account &rarr;</button>
      </div>
    </div>`;
  };

  const connectionMarkup = cachedConnections.length
    ? cachedConnections
        .map(
          (connection) => `
            <div class="beever-chatgpt-card" data-connection-id="${connection.id}">
              <div class="beever-chatgpt-row">
                <div>
                  <div style="display:flex;align-items:center;gap:0.75rem;">
                    ${chatgptIconMarkup()}
                    <div>
                      <h3>${escapeHtml(connection.display_name)}</h3>
                      <p style="margin-top:0.25rem;font-size:0.92rem;">ChatGPT history via plugin overlay</p>
                    </div>
                  </div>
                </div>
                <span class="beever-chatgpt-pill">${connection.status}</span>
              </div>
              <div class="beever-chatgpt-meta">
                <div class="beever-chatgpt-stat"><span>Selected</span><strong>${connection.selected_channels.length}</strong></div>
                <div class="beever-chatgpt-stat"><span>ChatGPT Access</span><strong>${getAccessLabel(status)}</strong></div>
                <div class="beever-chatgpt-stat"><span>History Cache</span><strong>${status?.total_conversations ?? 0}</strong></div>
              </div>
              ${connection.error_message ? `<div class="beever-chatgpt-error">${escapeHtml(connection.error_message)}</div>` : ""}
              ${fetchResults.has(connection.id) ? `<p style="margin-top:0.75rem;font-size:0.85rem;color:#0f8f70;">${escapeHtml(fetchResults.get(connection.id)!)}</p>` : ""}
              ${browserBannerMarkup(connection.id)}
              <div class="beever-chatgpt-actions">
                <button class="beever-chatgpt-button primary" data-action="manage" data-connection-id="${connection.id}">Manage Conversations</button>
                <button class="beever-chatgpt-button" data-action="fetch-history" data-connection-id="${connection.id}" ${fetchingConnections.has(connection.id) ? "disabled" : ""}>${fetchingConnections.has(connection.id) ? "Fetching…" : "Fetch from ChatGPT"}</button>
                <button class="beever-chatgpt-button" data-action="sync" data-connection-id="${connection.id}">Refresh Now</button>
                <button class="beever-chatgpt-button ghost" data-action="disconnect" data-connection-id="${connection.id}">Disconnect</button>
              </div>
            </div>
          `,
        )
        .join("")
    : `
      <div class="beever-chatgpt-card">
        <div class="beever-chatgpt-row">
          <div style="display:flex;align-items:center;gap:0.75rem;">
            ${chatgptIconMarkup()}
            <div>
              <h3>ChatGPT History</h3>
              <p style="margin-top:0.25rem;font-size:0.92rem;">Plugin-owned connection flow layered on top of the existing file-source pipeline.</p>
            </div>
          </div>
          <span class="beever-chatgpt-pill">Plugin</span>
        </div>
        <div class="beever-chatgpt-meta">
          <div class="beever-chatgpt-stat"><span>ChatGPT Access</span><strong>${getAccessLabel(status)}</strong></div>
          <div class="beever-chatgpt-stat"><span>Cached History</span><strong>${status?.total_conversations ?? 0}</strong></div>
          <div class="beever-chatgpt-stat"><span>Sync Interval</span><strong>${status?.sync_interval_hours ?? 6}h</strong></div>
        </div>
        ${browserBannerMarkup(null)}
        <div class="beever-chatgpt-actions">
          <button class="beever-chatgpt-button primary" data-action="connect">Connect ChatGPT History</button>
        </div>
      </div>
    `;

  section.innerHTML = connectionMarkup;
  section.querySelectorAll<HTMLButtonElement>("button[data-action='connect']").forEach((button) => {
    button.onclick = () => openWizard(null);
  });
  section.querySelectorAll<HTMLButtonElement>("button[data-action='manage']").forEach((button) => {
    const connection = cachedConnections.find((item) => item.id === button.dataset.connectionId);
    button.onclick = () => openWizard(connection ?? null);
  });
  section.querySelectorAll<HTMLButtonElement>("button[data-action='connect-account']").forEach((button) => {
    const connection = cachedConnections.find((item) => item.id === button.dataset.connectionId) ?? null;
    button.onclick = () => openWizard(connection);
  });
  section.querySelectorAll<HTMLButtonElement>("button[data-action='sync']").forEach((button) => {
    button.onclick = async () => {
      const connectionId = button.dataset.connectionId;
      if (!connectionId) return;
      button.disabled = true;
      button.textContent = "Refreshing…";
      try {
        await apiJson(`/api/plugins/chatgpt/connections/${connectionId}/sync`, "POST", {});
        window.location.reload();
      } catch (error) {
        alert(error instanceof Error ? error.message : "Failed to sync ChatGPT history");
        button.disabled = false;
        button.textContent = "Refresh Now";
      }
    };
  });
  section.querySelectorAll<HTMLButtonElement>("button[data-action='fetch-history']").forEach((button) => {
    button.onclick = async () => {
      const connectionId = button.dataset.connectionId;
      if (!connectionId || fetchingConnections.has(connectionId)) return;
      fetchingConnections.add(connectionId);
      fetchResults.delete(connectionId);
      _startProgressPoll();
      scheduleRefresh();
      try {
        const result = await apiJson<{ status: string; total: number; active: number; archived: number; pinned: number; in_projects: number }>(
          `/api/plugins/chatgpt/connections/${connectionId}/fetch-history`,
          "POST",
          {},
        );
        cachedStatus = {
          ...(cachedStatus ?? {
            enabled: true,
            history_file_exists: true,
            total_conversations: 0,
            connected_sources: cachedConnections.length,
            selected_conversations: 0,
            browser_available: true,
            browser_authenticated: true,
            browser_reason: null,
            sync_interval_hours: 6,
          }),
          history_file_exists: true,
          total_conversations: result.total,
        };
        fetchResults.set(connectionId, `✓ ${result.total} conversations (${result.active} active, ${result.pinned} pinned, ${result.in_projects} in projects)`);
      } catch (error) {
        fetchResults.set(connectionId, `✗ ${error instanceof Error ? error.message : "Failed to fetch"}`);
      } finally {
        fetchingConnections.delete(connectionId);
        _stopProgressPoll();
        scheduleRefresh();
      }
    };
  });
  section.querySelectorAll<HTMLButtonElement>("button[data-action='disconnect']").forEach((button) => {
    button.onclick = async () => {
      const connectionId = button.dataset.connectionId;
      if (!connectionId) return;
      if (!window.confirm("Disconnect ChatGPT History?")) {
        return;
      }
      button.disabled = true;
      try {
        await fetch(`${API_BASE}/api/connections/${connectionId}`, {
          method: "DELETE",
          headers: authHeaders(),
        });
        window.location.reload();
      } catch (error) {
        alert(error instanceof Error ? error.message : "Failed to delete ChatGPT connection");
        button.disabled = false;
      }
    };
  });
}

function injectWelcomeTile() {
  if (window.location.pathname !== "/") {
    return;
  }
  const grid = document.querySelector(".custom-platform-btn")?.parentElement;
  if (!grid) {
    document.getElementById(WELCOME_TILE_ID)?.remove();
    return;
  }
  if (document.getElementById(WELCOME_TILE_ID)) {
    return;
  }
  const button = document.createElement("button");
  button.id = WELCOME_TILE_ID;
  button.type = "button";
  button.className = "flex flex-col items-start gap-3 p-5 rounded-2xl border border-white/10 custom-platform-btn bg-background/40 hover:bg-background/80 hover:border-primary/40 hover:shadow-[0_8px_20px_rgba(0,0,0,0.08)] text-left transition-all duration-200 group cursor-pointer";
  button.innerHTML = `
    <div class="w-10 h-10 rounded-[14px] bg-primary/10 border border-primary/10 text-primary flex items-center justify-center group-hover:bg-primary group-hover:text-primary-foreground group-hover:scale-110 transition-all duration-200 shadow-sm">
      <span style="font-weight:800;">C</span>
    </div>
    <div>
      <p class="text-[15px] font-bold text-foreground tracking-tight group-hover:text-primary transition-colors">ChatGPT History</p>
      <p class="text-[12.5px] text-muted-foreground leading-snug mt-1">Reuse your ChatGPT browser session or cached history file.</p>
    </div>
  `;
  button.addEventListener("click", () => openWizard(null));
  grid.appendChild(button);
}

function injectPickerOption() {
  const title = findElementByText<HTMLHeadingElement>("h2", "Choose a platform");
  if (!title) {
    document.getElementById(PICKER_TILE_ID)?.remove();
    return;
  }
  const optionsHost = title.closest(".relative")?.querySelector(".p-3");
  if (!optionsHost || document.getElementById(PICKER_TILE_ID)) {
    return;
  }
  const button = document.createElement("button");
  button.id = PICKER_TILE_ID;
  button.type = "button";
  button.className = "beever-chatgpt-option";
  button.innerHTML = `
    ${chatgptIconMarkup()}
    <div>
      <div style="font-size:0.95rem;font-weight:600;color:var(--foreground, #12242b);">ChatGPT History</div>
      <div style="font-size:0.82rem;margin-top:0.2rem;">Connect browser-backed or cached ChatGPT conversations through the plugin layer.</div>
    </div>
  `;
  button.addEventListener("click", () => {
    button.closest(".fixed")?.remove();
    openWizard(null);
  });
  optionsHost.appendChild(button);
}

function ensureModalRoot() {
  let root = document.getElementById(MODAL_ROOT_ID) as HTMLDivElement | null;
  if (root) {
    return root;
  }
  root = document.createElement("div");
  root.id = MODAL_ROOT_ID;
  document.body.appendChild(root);
  return root;
}

function closeWizard() {
  document.getElementById(MODAL_ROOT_ID)?.remove();
}

function openWizard(connection: ChatGPTConnection | null) {
  const state: WizardState = {
    mode: connection ? "manage" : "create",
    connection,
    displayName: connection?.display_name || "ChatGPT History",
    authMode: "browser",
    channels: [],
    selected: new Set(connection?.selected_channels || []),
    loading: false,
    fetching: false,
    fetchResult: null,
    saving: false,
    error: null,
    step: connection
      ? (cachedStatus?.browser_authenticated ? "channels" : "token-import")
      : "setup",
    tokenSaving: false,
  };

  const root = ensureModalRoot();

  async function loadChannels() {
    if (!state.connection) {
      return;
    }
    state.loading = true;
    state.error = null;
    render();
    try {
      state.channels = await apiGet<ChatGPTChannel[]>(`/api/plugins/chatgpt/connections/${state.connection.id}/channels`);
    } catch (error) {
      state.error = error instanceof Error ? error.message : "Failed to load conversations";
    } finally {
      state.loading = false;
      render();
    }
  }

  async function handleFetchHistory() {
    if (!state.connection) return;
    state.fetching = true;
    state.fetchResult = null;
    state.error = null;
    _startProgressPoll();
    render();
    try {
      const result = await apiJson<{ status: string; total: number; active: number; pinned: number; in_projects: number }>(
        `/api/plugins/chatgpt/connections/${state.connection.id}/fetch-history`,
        "POST",
        {},
      );
      cachedStatus = {
        ...(cachedStatus ?? {
          enabled: true,
          history_file_exists: true,
          total_conversations: 0,
          connected_sources: cachedConnections.length,
          selected_conversations: 0,
          browser_available: true,
          browser_authenticated: true,
          browser_reason: null,
          sync_interval_hours: 6,
        }),
        history_file_exists: true,
        total_conversations: result.total,
      };
      state.fetchResult = `✓ ${result.total} conversations (${result.active} active, ${result.pinned} pinned, ${result.in_projects} in projects)`;
    } catch (error) {
      state.error = error instanceof Error ? error.message : "Failed to fetch history from ChatGPT";
    } finally {
      state.fetching = false;
      _stopProgressPoll();
      render();
    }
    await loadChannels();
  }

  async function handleTokenSave(raw: string) {
    state.tokenSaving = true;
    state.error = null;
    render();
    let token = raw.trim();
    try { const obj = JSON.parse(token); if (obj.accessToken) token = obj.accessToken as string; } catch { /* not JSON */ }
    if (!token || token.split(".").length < 3) {
      state.error = "This doesn't look like a valid ChatGPT session. Did you copy the right page?";
      state.tokenSaving = false;
      render();
      return;
    }
    try {
      await apiJson<{ status: string }>("/api/plugins/chatgpt/import-token", "POST", { token });
      try { cachedStatus = await apiGet<ChatGPTStatus>("/api/plugins/chatgpt/status"); } catch { /* ignore */ }
      state.step = "channels";
      state.tokenSaving = false;
      render();
      await handleFetchHistory();
    } catch (error) {
      state.error = error instanceof Error ? error.message : "Failed to save token";
      state.tokenSaving = false;
      render();
    }
  }

  async function handleConnect() {
    state.saving = true;
    state.error = null;
    render();
    try {
      state.connection = await apiJson<ChatGPTConnection>("/api/plugins/chatgpt/connect", "POST", {
        display_name: state.displayName,
        auth_mode: state.authMode,
      });
      state.selected = new Set(state.connection.selected_channels || []);
      state.saving = false;
      if (state.authMode === "browser" && !cachedStatus?.browser_authenticated) {
        state.step = "token-import";
        render();
      } else {
        state.step = "channels";
        render();
        await loadChannels();
      }
    } catch (error) {
      state.error = error instanceof Error ? error.message : "Failed to connect ChatGPT";
      state.saving = false;
      render();
    }
  }

  async function handleSave() {
    if (!state.connection) {
      return;
    }
    state.saving = true;
    state.error = null;
    render();
    try {
      await apiJson(`/api/plugins/chatgpt/connections/${state.connection.id}/channels`, "PUT", {
        selected_channels: Array.from(state.selected),
      });
      window.location.reload();
    } catch (error) {
      state.error = error instanceof Error ? error.message : "Failed to save conversations";
      state.saving = false;
      render();
    }
  }

  function bindModalEvents() {
    root.querySelectorAll<HTMLInputElement>("input[data-auth-mode]").forEach((input) => {
      input.onchange = () => {
        state.authMode = input.value === "file_only" ? "file_only" : "browser";
      };
    });
    const nameInput = root.querySelector<HTMLInputElement>("input[data-display-name]");
    if (nameInput) {
      nameInput.oninput = () => {
        state.displayName = nameInput.value;
      };
    }
    root.querySelectorAll<HTMLInputElement>("input[data-channel-id]").forEach((input) => {
      input.onchange = () => {
        const channelId = input.dataset.channelId;
        if (!channelId) {
          return;
        }
        if (input.checked) {
          state.selected.add(channelId);
        } else {
          state.selected.delete(channelId);
        }
      };
    });
    root.querySelector<HTMLButtonElement>("button[data-action='close']")?.addEventListener("click", closeWizard);
    root.querySelector<HTMLDivElement>(".beever-chatgpt-modal-backdrop")?.addEventListener("click", closeWizard);
    root.querySelector<HTMLButtonElement>("button[data-action='connect']")?.addEventListener("click", () => {
      void handleConnect();
    });
    root.querySelector<HTMLButtonElement>("button[data-action='save']")?.addEventListener("click", () => {
      void handleSave();
    });
    root.querySelector<HTMLButtonElement>("button[data-action='back']")?.addEventListener("click", () => {
      state.step = "setup";
      state.error = null;
      render();
    });
    root.querySelector<HTMLButtonElement>("button[data-action='fetch-history-modal']")?.addEventListener("click", () => {
      void handleFetchHistory();
    });
    root.querySelector<HTMLAnchorElement>("a[data-action='token-step1']")?.addEventListener("click", () => {
      const num = root.querySelector<HTMLElement>("#tok-num-1");
      if (num) { num.classList.add("done"); num.textContent = "\u2713"; }
    });
    root.querySelector<HTMLButtonElement>("button[data-action='token-paste']")?.addEventListener("click", async () => {
      let text: string;
      try {
        text = await navigator.clipboard.readText();
      } catch {
        state.error = "Could not access clipboard. Use the manual paste box below.";
        const details = root.querySelector<HTMLDetailsElement>("details");
        if (details) details.open = true;
        render();
        return;
      }
      if (!text.trim()) {
        state.error = "Clipboard is empty. Please go to chatgpt.com/api/auth/session, press Ctrl+A then Ctrl+C, then come back and click Paste & Connect.";
        render();
        return;
      }
      await handleTokenSave(text);
    });
    root.querySelector<HTMLButtonElement>("button[data-action='token-manual']")?.addEventListener("click", async () => {
      const raw = root.querySelector<HTMLTextAreaElement>("[data-manual-token]")?.value ?? "";
      await handleTokenSave(raw);
    });
  }

  function render() {
    const statusLine = cachedStatus
      ? cachedStatus.browser_authenticated
        ? cachedStatus.auth_source === "token_file"
          ? "Connected through an imported session token. Listing conversations works, but some chats may still require a live browser session to download full message content."
          : cachedStatus.auth_source === "browser_cookies"
            ? "Connected through a live authenticated browser session."
            : "Connected to ChatGPT."
        : cachedStatus.history_file_exists
          ? "No live ChatGPT session detected. Cached history is available."
          : "No authenticated ChatGPT session or cached history was detected yet."
      : "";
    root.innerHTML = `
      <div class="beever-chatgpt-modal">
        <div class="beever-chatgpt-modal-backdrop"></div>
        <div class="beever-chatgpt-modal-panel">
          <div class="beever-chatgpt-row">
            <div>
              <div style="display:flex;align-items:center;gap:0.75rem;">
                ${chatgptIconMarkup()}
                <div>
                  <h3>${state.mode === "create" ? "Connect ChatGPT History" : "Manage ChatGPT History"}</h3>
                  <p style="margin-top:0.3rem;font-size:0.92rem;">Plugin-owned connection flow layered on top of Atlas file-source ingestion.</p>
                </div>
              </div>
            </div>
            <button class="beever-chatgpt-button ghost" data-action="close">Close</button>
          </div>
          ${state.step === "setup" ? `
            <div style="margin-top:1rem;">
              <label>
                Connection name
                <input type="text" data-display-name value="${escapeAttribute(state.displayName)}" />
              </label>
              <div class="beever-chatgpt-option-list">
                <label class="beever-chatgpt-option">
                  <input type="radio" name="chatgpt-auth-mode" value="browser" data-auth-mode ${state.authMode === "browser" ? "checked" : ""} />
                  <div>
                    <strong style="display:block;color:var(--foreground, #12242b);">Use existing browser session</strong>
                    <span>Reuse an authenticated chatgpt.com browser session through the local browser bridge.</span>
                  </div>
                </label>
                <label class="beever-chatgpt-option">
                  <input type="radio" name="chatgpt-auth-mode" value="file_only" data-auth-mode ${state.authMode === "file_only" ? "checked" : ""} />
                  <div>
                    <strong style="display:block;color:var(--foreground, #12242b);">Use cached history file</strong>
                    <span>Connect from the existing chatgpt_history.json cache without probing the browser.</span>
                  </div>
                </label>
              </div>
              <p style="margin-top:1rem;font-size:0.9rem;">${escapeHtml(statusLine)}</p>
            </div>
          ` : state.step === "token-import" ? `
            <div style="margin-top:1rem;">
              <h4 style="font-size:1rem;margin:0 0 0.25rem;color:var(--foreground,#12242b);">Connect your ChatGPT account</h4>
              <p style="font-size:0.88rem;color:var(--muted-foreground,#5f7680);margin-bottom:1.25rem;">3 steps &mdash; takes about 30 seconds.</p>
              <div class="beever-chatgpt-token-steps">
                <div class="beever-chatgpt-token-step">
                  <div class="beever-chatgpt-token-num" id="tok-num-1">1</div>
                  <div>
                    <strong style="display:block;color:var(--foreground,#12242b);">Open your ChatGPT session</strong>
                    <p style="margin:0.3rem 0 0.6rem;font-size:0.85rem;">Make sure you are already logged in to ChatGPT, then click below.</p>
                    <a class="beever-chatgpt-button" href="https://chatgpt.com/api/auth/session" target="_blank" rel="noopener" data-action="token-step1">Open ChatGPT session &#8599;</a>
                  </div>
                </div>
                <div class="beever-chatgpt-token-step">
                  <div class="beever-chatgpt-token-num">2</div>
                  <div>
                    <strong style="display:block;color:var(--foreground,#12242b);">Copy all text on that page</strong>
                    <p style="font-size:0.85rem;margin:0.3rem 0 0;">Press <kbd style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:3px;padding:1px 5px;">Ctrl+A</kbd> then <kbd style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:3px;padding:1px 5px;">Ctrl+C</kbd>. It will look like random text &mdash; that is normal.</p>
                  </div>
                </div>
                <div class="beever-chatgpt-token-step">
                  <div class="beever-chatgpt-token-num">3</div>
                  <div>
                    <strong style="display:block;color:var(--foreground,#12242b);">Come back here and click Connect</strong>
                    <p style="font-size:0.85rem;margin:0.3rem 0 0.6rem;">Beever Atlas will read your clipboard and save your session.</p>
                    <button class="beever-chatgpt-button primary" data-action="token-paste" ${state.tokenSaving ? "disabled" : ""}>${state.tokenSaving ? "Connecting\u2026" : "Paste &amp; Connect"}</button>
                  </div>
                </div>
              </div>
              <details style="margin-top:1rem;">
                <summary style="cursor:pointer;font-size:0.82rem;color:var(--muted-foreground,#5f7680);user-select:none;">Clipboard blocked? Paste manually</summary>
                <textarea class="beever-chatgpt-manual-area" data-manual-token placeholder="Paste the page content here (Ctrl+V)\u2026"></textarea>
                <button class="beever-chatgpt-button" style="margin-top:0.5rem;" data-action="token-manual" ${state.tokenSaving ? "disabled" : ""}>Save pasted token</button>
              </details>
            </div>
          ` : `
            <div style="margin-top:1rem;">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:0.75rem;flex-wrap:wrap;">
                <p style="font-size:0.92rem;margin:0;">Choose which conversations Atlas should materialize as channels.</p>
                <button class="beever-chatgpt-button" data-action="fetch-history-modal" ${state.fetching ? "disabled" : ""}>${state.fetching ? `Fetching from ChatGPT\u2026 <span id="beever-chatgpt-modal-progress" style="opacity:0.75;"></span>` : "\u21bb Fetch from ChatGPT"}</button>
              </div>
              ${state.fetchResult ? `<p style="margin-top:0.5rem;font-size:0.85rem;color:#0f8f70;">${escapeHtml(state.fetchResult)}</p>` : ""}
              ${state.loading ? `<div style="padding:1.2rem 0;color:var(--muted-foreground, #5f7680);">Loading available conversations…</div>` : `
                <div class="beever-chatgpt-channel-list">
                  ${state.channels.map((channel) => `
                    <label class="beever-chatgpt-channel">
                      <input type="checkbox" data-channel-id="${escapeAttribute(channel.channel_id)}" ${state.selected.has(channel.channel_id) ? "checked" : ""} />
                      <div>
                        <strong style="display:block;color:var(--foreground, #12242b);">${escapeHtml(channel.name)}</strong>
                        <span>${escapeHtml(channel.topic || "ChatGPT conversation")}</span>
                      </div>
                    </label>
                  `).join("") || `<div style="padding:1rem 0;color:var(--muted-foreground, #5f7680);">No conversations discovered. Click "↻ Fetch from ChatGPT" to load your history.</div>`}
                </div>
              `}
            </div>
          `}
          ${state.error ? `<div class="beever-chatgpt-error">${escapeHtml(state.error)}</div>` : ""}
          <div class="beever-chatgpt-actions" style="justify-content:flex-end;">
            ${state.step === "channels" && state.mode === "create" ? `<button class="beever-chatgpt-button ghost" data-action="back">Back</button>` : ""}
            ${state.step === "setup"
              ? `<button class="beever-chatgpt-button primary" data-action="connect" ${state.saving ? "disabled" : ""}>${state.saving ? "Connecting\u2026" : "Continue"}</button>`
              : state.step === "channels"
              ? `<button class="beever-chatgpt-button primary" data-action="save" ${state.saving ? "disabled" : ""}>${state.saving ? "Saving\u2026" : "Save Conversations"}</button>`
              : ""}
          </div>
        </div>
      </div>
    `;
    bindModalEvents();
  }

  render();
  if (state.step === "channels") {
    void loadChannels();
  }
}

function escapeHtml(value: string) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttribute(value: string) {
  return escapeHtml(value);
}

function installNavigationHooks() {
  const dispatch = () => window.dispatchEvent(new Event("beever:locationchange"));
  const pushState = history.pushState.bind(history);
  const replaceState = history.replaceState.bind(history);

  history.pushState = ((...args) => {
    const result = pushState(...args);
    dispatch();
    return result;
  }) as typeof history.pushState;

  history.replaceState = ((...args) => {
    const result = replaceState(...args);
    dispatch();
    return result;
  }) as typeof history.replaceState;

  window.addEventListener("popstate", dispatch);
  window.addEventListener("beever:locationchange", scheduleRefresh);
  window.addEventListener("connections-changed", scheduleRefresh);
}

/** Poll until React renders the page content we need, then stop. */
function waitForPageContent() {
  if (!isRelevantPage()) {
    return;
  }
  const hasSettingsContent = !!findElementByText<HTMLHeadingElement>("h2", "Platform Connections");
  const hasHomeContent = !!document.querySelector(".custom-platform-btn");
  if (hasSettingsContent || hasHomeContent) {
    injectSettingsSection();
    injectSettingsHeaderButton();
    injectPickerOption();
    injectWelcomeTile();
    hideNativeFileCards();
    return;
  }
  // React hasn't rendered yet — retry shortly (stops when content appears)
  window.setTimeout(waitForPageContent, 120);
}

function boot() {
  installStyles();
  installNavigationHooks();
  scheduleRefresh();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}