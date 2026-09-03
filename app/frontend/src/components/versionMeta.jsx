import React from "react";
import { Badge } from "./index.jsx";

// Shared presentation helpers for a version's registry identity. Used by both the
// Versions list (table) and the Version graph so status colors and provenance read
// identically in either view.

// status → badge tone. Mirrors the kernel status lifecycle (registry.py):
// promoted/restored/reapplied are "good", committed/abandoned are neutral, the
// warn set left the active line but is recoverable, the bad set never shipped.
export const STATUS_TONES = {
  promoted: "good",
  restored: "good",
  reapplied: "good",
  line_promoted: "accent",
  committed: "muted",
  abandoned: "muted",
  pending: "warn",
  rolled_back: "warn",
  reverted: "warn",
  rejected: "bad",
  health_failed: "bad",
  verify_failed: "bad",
  eval_failed: "bad",
};

// The tone a node/badge should paint with. Active always wins (accent).
export function toneOf(v, isActive) {
  if (isActive) return "accent";
  return STATUS_TONES[v?.status] || "";
}

export function StatusBadge({ v, isActive }) {
  if (isActive) return <Badge tone="accent">active</Badge>;
  if (!v.status) return null;
  const label = v.status === "line_promoted" ? "on line" : v.status.replace("_", " ");
  return <Badge tone={STATUS_TONES[v.status] || ""}>{label}</Badge>;
}

// The Verification Gate's verdict on a version, when it ran (kernel `verification`
// registry field): passed n/n checks, failed, or promoted unverified (derivation
// skipped / failed fail-open). Absent whenever the gate is off — render nothing.
export function VerificationBadge({ v }) {
  const ver = v?.verification;
  if (!ver) return null;
  if (ver.unverified) {
    return <Badge tone="warn" title={ver.reason || "no acceptance checks were derived"}>unverified</Badge>;
  }
  if (ver.ok) {
    return <Badge tone="good" title="passed acceptance + regression checks">◎ {ver.passed}/{ver.total} checks</Badge>;
  }
  const first = (ver.failed && ver.failed[0]) || {};
  return <Badge tone="bad" title={first.detail || "verification failed"}>◎ checks failed</Badge>;
}

// "self-mod · t3f9a…" / "revert of v7" / "re-applies v9" / "seed" — the same edge
// data the graph draws as dashed cross-links, rendered as text.
export function Origin({ v, seqOf }) {
  const bits = [];
  if (v.origin === "revert" && v.reverts) bits.push(`revert of ${seqOf(v.reverts)}`);
  else if (v.origin === "reapply" && v.reapplies) bits.push(`re-applies ${seqOf(v.reapplies)}`);
  else if (v.origin) bits.push(v.origin === "self-mod" ? "self-mod" : v.origin);
  if (v.task) bits.push(v.task.slice(0, 8) + "…");
  if (v.reverted_by) bits.push(`reverted by ${seqOf(v.reverted_by)}`);
  if (v.reapplied_by) bits.push(`re-applied as ${seqOf(v.reapplied_by)}`);
  if (bits.length === 0) return null;
  return <span className="ver-origin muted">{bits.join(" · ")}</span>;
}
