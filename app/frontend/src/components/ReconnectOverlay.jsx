import React, { useEffect, useState } from "react";
import Logo from "../Logo.jsx";

// Full-screen "Reconnecting…" splash shown during a blue-green reboot. api.js dispatches
// `quine:reconnecting` (on the first 503 from the proxy, or explicitly after a self-mod
// promote/rollback) and then polls /health until the new slot answers and reloads the page.
// This replaces both the raw kernel "app is rebooting…" text and the old fixed-timer reloads
// that could race a slow boot. It never unmounts itself — the readiness poll owns the reload.
export default function ReconnectOverlay() {
  const [state, setState] = useState(null); // { reason } | null

  useEffect(() => {
    const onReconnect = (e) => setState({ reason: (e.detail && e.detail.reason) || "" });
    window.addEventListener("quine:reconnecting", onReconnect);
    return () => window.removeEventListener("quine:reconnecting", onReconnect);
  }, []);

  if (!state) return null;
  return (
    <div className="reconnect-overlay" role="alertdialog" aria-live="assertive" aria-busy="true">
      <div className="reconnect-card">
        <Logo size={44} animate className="reconnect-logo" />
        <div className="reconnect-title">{state.reason || "Reconnecting…"}</div>
        <div className="reconnect-sub">This page will refresh automatically when it’s ready.</div>
        <div className="reconnect-bar" aria-hidden="true">
          <span />
        </div>
      </div>
    </div>
  );
}
