// Thin helpers over the two surfaces this app talks to:
//   /api/syscall/*  → privileged kernel operations (status, versions, config, …)
//   /api/*          → this app's own backend (the agent / conversations)
//
// The app may be served at the origin root or below a reverse-proxy path prefix. Derive that
// mount prefix at runtime so every request resolves under it instead of escaping to the origin.
// Tabs are state, not URL routes, so location.pathname is the mount root; if client-side routing
// is added later, switch to deriving the prefix from import.meta.url.
export const PREFIX = window.location.pathname.replace(/\/+$/, "");
export const apiUrl = (path) => PREFIX + path;

const SYS = apiUrl("/api/syscall");

// Blue-green reboot bridge. During a reboot the gateway has no live app slot and answers
// every proxied request with a bare 503 ("app is rebooting…"). Rather than let that surface
// as a silent failure or a flash of raw kernel text, the first 503 we see raises a branded
// "Reconnecting…" overlay (see components/ReconnectOverlay.jsx) and polls /health until the
// new slot answers, then reloads into it. Idempotent — only the first 503 starts the poll.
let _reconnecting = false;

export function beginReconnect(reason) {
  if (_reconnecting) return;
  _reconnecting = true;
  window.dispatchEvent(new CustomEvent("quine:reconnecting", { detail: { reason: reason || "" } }));
  const health = apiUrl("/health");
  const poll = async () => {
    try {
      const r = await window.fetch(health, { cache: "no-store" });
      if (r.ok) return window.location.reload();
    } catch {
      /* slot not accepting connections yet */
    }
    setTimeout(poll, 600);
  };
  setTimeout(poll, 600);
}

// fetch wrapper: any 503 from the proxy means "mid-reboot" → kick off the reconnect flow.
async function _fetch(url, opts) {
  const r = await window.fetch(url, opts);
  if (r.status === 503) beginReconnect();
  return r;
}

export async function sysGet(path) {
  const r = await _fetch(SYS + path);
  return r.json();
}

export async function sysPost(path, body) {
  const r = await _fetch(SYS + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return { status: r.status, data: await r.json().catch(() => ({})) };
}

export async function appGet(path) {
  const r = await _fetch(apiUrl(path));
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status}`);
  return r.json();
}

export async function appPost(path, body) {
  const r = await _fetch(apiUrl(path), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

export async function appDelete(path) {
  const r = await _fetch(apiUrl(path), { method: "DELETE" });
  return r.json();
}

export async function appPut(path, body) {
  const r = await _fetch(apiUrl(path), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

// Multipart upload of a single File (used by the Knowledge tab). The browser sets the
// multipart Content-Type (with boundary) automatically — don't set it by hand.
export async function appUpload(path, file, field = "file") {
  const fd = new FormData();
  fd.append(field, file);
  const r = await _fetch(apiUrl(path), { method: "POST", body: fd });
  return r.json();
}

// Read a Server-Sent-Events response body, calling onEvent(obj) per `data:` JSON frame.
// Lines starting with ":" are comments/heartbeats and are skipped. Shared by postStream (the
// sender's own run) and getStream (a read-only watcher following someone else's run).
async function _readSSE(resp, path, onEvent, signal) {
  if (!resp.ok || !resp.body) {
    let detail = "";
    try {
      detail = JSON.stringify(await resp.json());
    } catch {
      /* ignore */
    }
    throw new Error(`stream ${path} -> ${resp.status} ${detail}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let aborted = false;
  if (signal) {
    signal.addEventListener(
      "abort",
      () => {
        aborted = true;
        reader.cancel().catch(() => {});
      },
      { once: true },
    );
  }
  for (;;) {
    const { value, done } = await reader.read();
    if (done || aborted) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (aborted) break;
        const s = line.trim();
        if (s.startsWith("data:")) {
          const payload = s.slice(5).trim();
          if (!payload) continue;
          try {
            onEvent(JSON.parse(payload));
          } catch {
            /* ignore malformed frame */
          }
        }
      }
      if (aborted) break;
    }
  }
}

// POST that returns a Server-Sent-Events stream; calls onEvent(obj) per `data:` JSON
// frame. (EventSource only supports GET, so we read the response body ourselves.)
export async function postStream(path, body, onEvent, signal) {
  const resp = await _fetch(apiUrl(path), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
    signal,
  });
  await _readSSE(resp, path, onEvent, signal);
}

// GET a long-lived SSE stream (read-only). Used to WATCH a conversation's live agent run that
// someone else started — the connection stays open across runs until `signal` aborts (e.g. when
// switching conversations or unmounting), so a heartbeat-only idle period is normal.
export async function getStream(path, onEvent, signal) {
  const resp = await _fetch(apiUrl(path), { signal });
  await _readSSE(resp, path, onEvent, signal);
}
