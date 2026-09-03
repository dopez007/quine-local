import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { ToastProvider } from "./components/Toast.jsx";
import { ConfirmProvider } from "./components/Confirm.jsx";
import "./theme.css";

// One-time migration of pre-rebrand localStorage keys (aimprove-* → quine-*) so saved
// preferences survive. The theme key is migrated inside useTheme.js.
(function migrateLegacyKeys() {
  const pairs = [
    ["aimprove-run-model", "quine-run-model"],
    ["aimprove-selfmod-prompt", "quine-selfmod-prompt"],
    ["aimprove-selfmod-steer", "quine-selfmod-steer"],
    ["aimprove-backend-max-rounds", "quine-backend-max-rounds"],
  ];
  for (const [oldK, newK] of pairs) {
    try {
      const v = localStorage.getItem(oldK);
      if (v != null && localStorage.getItem(newK) == null) localStorage.setItem(newK, v);
      localStorage.removeItem(oldK);
    } catch {
      /* ignore */
    }
  }
})();

// Global JS error reporter → the harness error tracker (see the Errors tab). Best-effort
// and rate-limited so a render-loop crash can't flood the store; fetch failures are ignored
// (never let the reporter itself throw).
(function installErrorReporter() {
  let reported = 0;
  const report = (message, extra) => {
    if (reported >= 10) return; // per-page-load cap
    reported += 1;
    try {
      window.fetch((window.location.pathname.replace(/\/+$/, "")) + "/api/errors/report", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          message: String(message || "unknown frontend error").slice(0, 2000),
          exc_type: "FrontendError",
          source: "manual",
          context: { frontend: true, url: window.location.href, ...extra },
        }),
      }).catch(() => {});
    } catch {
      /* ignore */
    }
  };
  window.addEventListener("error", (e) =>
    report(e.message, { file: e.filename, line: e.lineno }),
  );
  window.addEventListener("unhandledrejection", (e) =>
    report(e.reason && (e.reason.stack || e.reason.message || e.reason), { rejection: true }),
  );
})();

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ToastProvider>
      <ConfirmProvider>
        <App />
      </ConfirmProvider>
    </ToastProvider>
  </React.StrictMode>,
);
