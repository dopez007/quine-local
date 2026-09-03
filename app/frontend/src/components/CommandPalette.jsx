import React, { useEffect, useRef, useState } from "react";

// Cmd/Ctrl+K command palette. App.jsx owns the open state and supplies `commands`
// (each { id, label, hint?, action }). Filter by typing; ↑/↓ to move, ↵ to run, Esc to close.
export default function CommandPalette({ open, onClose, commands }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  const filtered = commands.filter((c) => {
    const s = q.trim().toLowerCase();
    if (!s) return true;
    return (c.label + " " + (c.hint || "")).toLowerCase().includes(s);
  });

  useEffect(() => {
    if (open) {
      setQ("");
      setSel(0);
      const t = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [open]);

  useEffect(() => setSel(0), [q]);

  useEffect(() => {
    listRef.current?.querySelector(".cmdk-item.active")?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!open) return null;

  function run(cmd) {
    if (!cmd) return;
    onClose();
    cmd.action();
  }

  function onKey(e) {
    if (e.key === "Escape") {
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(s + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(filtered[sel]);
    }
  }

  return (
    <div className="cmdk-backdrop" onClick={onClose}>
      <div
        className="cmdk"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          className="cmdk-input"
          placeholder="Type a command…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          aria-label="Command search"
        />
        <div className="cmdk-list" ref={listRef}>
          {filtered.length === 0 && <div className="cmdk-empty muted">No matching commands</div>}
          {filtered.map((c, i) => (
            <button
              key={c.id}
              className={"cmdk-item" + (i === sel ? " active" : "")}
              onMouseMove={() => setSel(i)}
              onClick={() => run(c)}
            >
              <span className="cmdk-label">{c.label}</span>
              {c.hint && <span className="cmdk-hint">{c.hint}</span>}
            </button>
          ))}
        </div>
        <div className="cmdk-foot muted">
          <span>↑↓ navigate</span>
          <span>↵ select</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}
