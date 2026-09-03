import React, { createContext, useCallback, useContext, useState } from "react";
import { Modal, Button } from "./index.jsx";

// Promise-based confirm dialog, built on the existing <Modal>. Wrap the app in
// <ConfirmProvider> (see main.jsx); then `const confirm = useConfirm()` and
// `if (await confirm({ title, body, danger: true })) { … }`. Passing a plain string is a
// shorthand for { body }. Replaces native window.confirm() with an on-brand dialog.
const ConfirmCtx = createContext(null);

export function ConfirmProvider({ children }) {
  const [state, setState] = useState(null); // { opts, resolve } | null

  const confirm = useCallback((opts) => {
    const normalized = typeof opts === "string" ? { body: opts } : opts || {};
    return new Promise((resolve) => setState({ opts: normalized, resolve }));
  }, []);

  const settle = useCallback((result) => {
    setState((s) => {
      s?.resolve(result);
      return null;
    });
  }, []);

  const opts = state?.opts || {};

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {state && (
        <Modal title={opts.title || "Are you sure?"} onClose={() => settle(false)}>
          {opts.body && <div className="confirm-body">{opts.body}</div>}
          <div className="confirm-actions">
            <Button onClick={() => settle(false)}>{opts.cancelLabel || "Cancel"}</Button>
            <Button variant={opts.danger ? "danger" : "primary"} onClick={() => settle(true)}>
              {opts.confirmLabel || (opts.danger ? "Delete" : "Confirm")}
            </Button>
          </div>
        </Modal>
      )}
    </ConfirmCtx.Provider>
  );
}

export function useConfirm() {
  const ctx = useContext(ConfirmCtx);
  if (!ctx) throw new Error("useConfirm must be used within a ConfirmProvider");
  return ctx;
}
