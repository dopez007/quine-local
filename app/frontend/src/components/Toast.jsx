import React, { createContext, useCallback, useContext, useRef, useState } from "react";

// Lightweight global toast/notification system. Wrap the app in <ToastProvider> (see main.jsx)
// and call useToast() anywhere: toast.ok("Saved"), toast.err("Failed: …"), toast.warn(…),
// toast.info(…). Toasts auto-dismiss (errors linger longer) and are manually dismissable.
const ToastCtx = createContext(null);

let idSeq = 0;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const timers = useRef({});

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id));
    const tm = timers.current[id];
    if (tm) {
      clearTimeout(tm);
      delete timers.current[id];
    }
  }, []);

  const push = useCallback(
    (tone, text, ttl) => {
      if (!text) return;
      const id = ++idSeq;
      setToasts((t) => [...t, { id, tone, text: String(text) }]);
      if (ttl !== 0) timers.current[id] = setTimeout(() => dismiss(id), ttl || 4500);
      return id;
    },
    [dismiss],
  );

  // Stable API object — methods close over the stable push/dismiss callbacks.
  const api = useRef({
    ok: (t, ttl) => push("ok", t, ttl),
    err: (t, ttl) => push("err", t, ttl ?? 8000),
    warn: (t, ttl) => push("warn", t, ttl),
    info: (t, ttl) => push("info", t, ttl),
    dismiss,
  }).current;

  return (
    <ToastCtx.Provider value={api}>
      {children}
      <div className="toast-stack" role="region" aria-live="polite" aria-label="Notifications">
        {toasts.map((t) => (
          <div key={t.id} className={"toast " + t.tone} role="status">
            <span className="toast-dot" aria-hidden="true" />
            <span className="toast-text">{t.text}</span>
            <button
              className="toast-close"
              aria-label="Dismiss notification"
              onClick={() => dismiss(t.id)}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used within a ToastProvider");
  return ctx;
}
