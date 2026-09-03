import React, { useEffect, useRef, useState } from "react";

// Reusable UI primitives. Tabs import from "../components"; reuse these (and the CSS
// variables in theme.css) rather than reinventing styles.

// Overlay dialog. Closes on ✕, backdrop click, or Esc. `wide` widens the panel (used
// by the diff viewer). Body scroll is locked while open.
export function Modal({ title, actions, onClose, wide = false, children }) {
  const panelRef = useRef(null);
  useEffect(() => {
    const prevFocused = document.activeElement;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const panel = panelRef.current;
    const focusable = () =>
      panel
        ? [
            ...panel.querySelectorAll(
              'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])',
            ),
          ]
        : [];
    (focusable()[0] || panel)?.focus();

    const onKey = (e) => {
      if (e.key === "Escape") {
        onClose?.();
        return;
      }
      if (e.key === "Tab") {
        const f = focusable();
        if (f.length === 0) {
          e.preventDefault();
          panel?.focus();
          return;
        }
        const first = f[0];
        const last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      prevFocused?.focus?.();
    };
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        ref={panelRef}
        className={"modal" + (wide ? " wide" : "")}
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2>{title}</h2>
          <div className="row">
            {actions}
            <button className="btn ghost icon" onClick={onClose} title="Close (Esc)" aria-label="Close dialog">
              ✕
            </button>
          </div>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function Card({ title, actions, children, className = "" }) {
  return (
    <section className={"card " + className}>
      {(title || actions) && (
        <div className="card-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Button({ variant = "ghost", children, ...props }) {
  return (
    <button className={"btn " + variant} {...props}>
      {children}
    </button>
  );
}

export function Badge({ tone = "", title, children }) {
  return <span className={"badge " + tone} title={title}>{children}</span>;
}

const HARNESS_STATE_LABELS = {
  idle: "Agent idle",
  running: "Agent running",
  pending: "Awaiting approval",
  success: "Run completed",
  failed: "Run failed",
  stopped: "Run stopped",
  queued: "Run queued",
  warning: "Attention needed",
};

/** A semantic agent-state indicator. The label keeps status meaningful without color. */
export function HarnessStatusDot({ state = "idle", label, title }) {
  const text = label || HARNESS_STATE_LABELS[state] || HARNESS_STATE_LABELS.idle;
  return (
    <span className={"harness-status-dot " + state} aria-label={text} title={title || text}>
      <span aria-hidden="true" />
    </span>
  );
}

/** Compact operational metadata used by both agent workbenches. */
export function HarnessMetric({ label, value, mono = false, title, className = "" }) {
  return (
    <span className={"harness-metric " + className} title={title}>
      <span className={"harness-metric-value" + (mono ? " mono" : "")}>{value ?? "—"}</span>
      <span className="harness-metric-label">{label}</span>
    </span>
  );
}

/** Shared top rail for mission state, proof-of-work metrics, and run controls. */
export function HarnessStatusBar({
  state = "idle",
  eyebrow = "Agent harness",
  title,
  summary,
  metrics,
  actions,
  children,
  className = "",
}) {
  return (
    <section className={"harness-status-bar " + className} aria-label={`${title || "Agent"} status`}>
      <div className="harness-status-row">
        <div className="harness-status-identity">
          <HarnessStatusDot state={state} />
          <div className="harness-status-copy">
            <span className="harness-status-eyebrow">{eyebrow}</span>
            <strong className="harness-status-title">{title}</strong>
            {summary && <span className="harness-status-summary">{summary}</span>}
          </div>
        </div>
        {metrics && <div className="harness-status-metrics">{metrics}</div>}
        {actions && <div className="harness-status-actions">{actions}</div>}
      </div>
      {children && <div className="harness-status-detail">{children}</div>}
    </section>
  );
}

/** Progressive disclosure for dense governance/history panels below the live workbench. */
export function HarnessDisclosure({
  title,
  description,
  badge,
  defaultOpen = false,
  attention = false,
  children,
  className = "",
}) {
  const [open, setOpen] = useState(defaultOpen || attention);
  useEffect(() => {
    if (attention) setOpen(true);
  }, [attention]);
  return (
    <details
      className={"harness-disclosure" + (attention ? " attention" : "") + ` ${className}`}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <span className="harness-disclosure-heading">
          <strong>{title}</strong>
          {description && <span>{description}</span>}
        </span>
        <span className="harness-disclosure-meta">
          {badge}
          <span className="harness-disclosure-chevron" aria-hidden="true">⌄</span>
        </span>
      </summary>
      <div className="harness-disclosure-body">{children}</div>
    </details>
  );
}

export function Field({ label, hint, children }) {
  return (
    <label className="field">
      {label && <span className="field-label">{label}</span>}
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function TextInput(props) {
  return <input className="input" {...props} />;
}

export function TextArea(props) {
  return <textarea className="textarea" {...props} />;
}

export function Select({ options = [], ...props }) {
  return (
    <select className="select" {...props}>
      {options.map((o) =>
        typeof o === "string" ? (
          <option key={o} value={o}>
            {o}
          </option>
        ) : (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ),
      )}
    </select>
  );
}

export function Spinner({ label }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" />
      {label && <span className="muted">{label}</span>}
    </span>
  );
}

export function Empty({ children }) {
  return (
    <div className="empty muted">
      <svg
        className="empty-icon"
        width="22"
        height="22"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="3" y="5" width="18" height="14" rx="2" />
        <path d="M3 10h18M9 15h6" />
      </svg>
      <span>{children}</span>
    </div>
  );
}

export function Pre({ children, className = "", innerRef }) {
  return (
    <pre ref={innerRef} className={"pre " + className}>
      {children}
    </pre>
  );
}

// Chat primitives
export function Message({ role, label = role, children }) {
  return (
    <div className={"msg " + role}>
      <div className="msg-role">{label}</div>
      <div className="msg-body">{children}</div>
    </div>
  );
}

export function ToolChip({ name, status = "", detail }) {
  const label = status === "run" ? "running" : status === "done" ? "done" : status === "failed" ? "failed" : "";
  return (
    <div className={"toolchip " + status}>
      <span className="toolchip-name">{name}</span>
      {label && <span className="toolchip-status">{label}</span>}
      {detail && <span className="toolchip-detail">{detail}</span>}
    </div>
  );
}

// Shared mapping of harness event/log kinds → a color tone + icon. Used by the Audit
// table and the Self-Modify live log so both read consistently.
const EVENT_TONES = {
  request: { tone: "accent", icon: "→" },
  change_request: { tone: "accent", icon: "→" },
  thought: { tone: "muted", icon: "~" },
  assistant: { tone: "", icon: ">" },
  tool: { tone: "", icon: ">" },
  tool_call: { tone: "accent", icon: ">" },
  tool_result: { tone: "", icon: "→" },
  steer: { tone: "accent", icon: "→" },
  steer_received: { tone: "accent", icon: "→" },
  stdout: { tone: "muted", icon: ">" },
  start: { tone: "muted", icon: ">" },
  engine_error: { tone: "bad", icon: "!" },
  end_no_tools: { tone: "warn", icon: "■" },
  propose: { tone: "warn", icon: "~" },
  propose_commit: { tone: "warn", icon: "~" },
  committed: { tone: "good", icon: "→" },
  version_committed: { tone: "good", icon: "→" },
  reboot: { tone: "accent", icon: "↻" },
  reboot_begin: { tone: "accent", icon: "↻" },
  promoted: { tone: "good", icon: "↑" },
  boot_ok: { tone: "good", icon: "↑" },
  runtime_fallback: { tone: "warn", icon: "←" },
  rolled_back: { tone: "bad", icon: "←" },
  rollback: { tone: "bad", icon: "←" },
  cancelled: { tone: "warn", icon: "/" },
  interrupted: { tone: "warn", icon: "!" },
  error: { tone: "bad", icon: "!" },
  worker_error: { tone: "bad", icon: "!" },
  boot_failed: { tone: "bad", icon: "!" },
  done: { tone: "good", icon: "●" },
  // git ops (selective revert / re-apply) + registry / queue / audit events
  revert_request: { tone: "accent", icon: "⎌" },
  reapply_request: { tone: "accent", icon: "⤴" },
  revert_conflict: { tone: "bad", icon: "!" },
  reapply_conflict: { tone: "bad", icon: "!" },
  label_set: { tone: "muted", icon: "🏷" },
  dequeued: { tone: "warn", icon: "/" },
  queue_resume: { tone: "muted", icon: "»" },
  pending: { tone: "warn", icon: "…" },
  promotion_pending: { tone: "warn", icon: "…" },
  promotion_approved: { tone: "good", icon: "✓" },
  promotion_rejected: { tone: "bad", icon: "✗" },
  health_failed: { tone: "bad", icon: "!" },
  boot_health_failed: { tone: "bad", icon: "!" },
  monitor_unhealthy: { tone: "bad", icon: "♥" },
  config_updated: { tone: "muted", icon: "⚙" },
  auth_denied: { tone: "bad", icon: "⛔" },
  // Verification Gate: derive → run acceptance + regression checks → pass/fail/freeze
  verify_derive: { tone: "accent", icon: "◎" },
  verify_derived: { tone: "accent", icon: "◎" },
  verify_skipped: { tone: "muted", icon: "◎" },
  verify_derive_failed: { tone: "warn", icon: "◎" },
  verify: { tone: "accent", icon: "◎" },
  verify_passed: { tone: "good", icon: "◎" },
  verify_failed: { tone: "bad", icon: "◎" },
  check_frozen: { tone: "good", icon: "❄" },
  check_toggled: { tone: "muted", icon: "◎" },
  checks_lifecycle: { tone: "muted", icon: "◎" },
  // Preview environments + named lines
  line: { tone: "good", icon: "⑂" },
  line_created: { tone: "accent", icon: "⑂" },
  line_advanced: { tone: "good", icon: "⑂" },
  line_promoted: { tone: "good", icon: "↑" },
  line_deleted: { tone: "warn", icon: "⑂" },
  preview_started: { tone: "accent", icon: "◫" },
  preview_stopped: { tone: "muted", icon: "◫" },
  preview_reaped: { tone: "muted", icon: "◫" },
  preview_failed: { tone: "bad", icon: "◫" },
  preview_verify_failed: { tone: "bad", icon: "◫" },
  // Autonomous triggers / self-healing
  trigger_fired: { tone: "accent", icon: "⚡" },
  trigger_saved: { tone: "muted", icon: "⚡" },
  trigger_toggled: { tone: "muted", icon: "⚡" },
  trigger_deleted: { tone: "warn", icon: "⚡" },
  trigger_skipped: { tone: "muted", icon: "⚡" },
  trigger_hold_unverified: { tone: "warn", icon: "⚡" },
  webhook_denied: { tone: "bad", icon: "⛔" },
  // Gated Kernel Self-Update — the substrate rewriting itself
  kernel_change_request: { tone: "accent", icon: "⬡" },
  kernel_version_committed: { tone: "good", icon: "⬡" },
  kernel_validate_failed: { tone: "bad", icon: "⬡" },
  kernel_promotion_pending: { tone: "warn", icon: "⬡" },
  kernel_promotion_approved: { tone: "good", icon: "⬡" },
  kernel_promotion_rejected: { tone: "bad", icon: "⬡" },
  kernel_approve_denied: { tone: "bad", icon: "⛔" },
  kernel_promoted: { tone: "good", icon: "⬆" },
  kernel_health_failed: { tone: "bad", icon: "!" },
  kernel_rolled_back: { tone: "warn", icon: "←" },
  kernel_rollback_requested: { tone: "warn", icon: "←" },
};

export function eventTone(kind = "") {
  return EVENT_TONES[kind] || { tone: "", icon: "•" };
}
