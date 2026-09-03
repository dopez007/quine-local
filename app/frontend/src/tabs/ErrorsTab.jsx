import React, { useEffect, useState } from "react";
import { appGet, appPost, appDelete, sysGet, sysPost } from "../api.js";
import { Card, Button, Badge, Empty, Pre } from "../components";
import { useConfirm } from "../components/Confirm.jsx";
import { useToast } from "../components/Toast.jsx";

// The prebuilt self-healing trigger the Errors-tab one-click toggle manages: an error-spike
// trigger that files a fix task when an unresolved group crosses the threshold. The fix is
// held for approval by default (autonomy posture) — you review the overnight fix, then click.
const SELFHEAL_NAME = "self-heal";
const SELFHEAL_SPEC = {
  name: SELFHEAL_NAME,
  kind: "error_spike",
  config: { threshold: 3, window_minutes: 60 },
  prompt_template:
    "Investigate and fix the recorded runtime error with fingerprint {{error.fingerprint}}: " +
    "{{error.exc_type}}: {{error.message}} (route {{error.route}}, {{error.count}} occurrences). " +
    "Most recent traceback:\n{{error.traceback_tail}}\n" +
    "After the fix is committed, call resolve_error(\"{{error.fingerprint}}\").",
};

// The error tracker ("Sentry for the harness"): runtime errors grouped by fingerprint —
// unhandled backend exceptions, chat-tool failures, manual reports — plus versions that
// failed their boot health check (with the captured crash log). Each group can be handed
// straight to the Self-Modify agent via "Fix with agent".

function ts(t) {
  if (!t) return "—";
  try {
    return new Date(t * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

function sourceTone(source) {
  if (source === "boot") return "bad";
  if (source === "run-agent") return "warn";
  return "accent";
}

// The digest handed to the Self-Modify agent. SelfModifyTab initialises its composer from
// this localStorage key on mount, so writing it + navigating prefills the prompt for the
// user to review/edit before submitting (never auto-submits).
function fixPrompt(g) {
  const lines = [];
  if (g.source === "boot") {
    const sha = (g.versions && g.versions[0]) || "?";
    lines.push(
      `Fix the boot failure of version ${sha.slice(0, 12)} (error tracker fingerprint ${g.fingerprint}).`,
      "",
      `The version failed its boot health check: ${g.message}`,
    );
  } else {
    lines.push(
      `Fix this recorded runtime error (error tracker fingerprint ${g.fingerprint}):`,
      "",
      `${g.exc_type}: ${g.message}`,
      `source: ${g.source}${g.route ? `, route: ${g.route}` : ""}, occurrences: ${g.count}, ` +
        `versions: ${(g.versions || []).map((v) => v.slice(0, 8)).join(", ") || "?"}`,
    );
  }
  if (g.last_traceback) {
    lines.push("", "Most recent traceback / crash log:", "```", g.last_traceback.slice(0, 3000), "```");
  }
  lines.push(
    "",
    "Use get_errors for more occurrences if needed. After the fix is committed" +
      (g.source === "boot" ? "." : `, call resolve_error(\"${g.fingerprint}\").`),
  );
  return lines.join("\n");
}

export default function ErrorsTab({ onNavigate }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [groups, setGroups] = useState([]);
  const [summary, setSummary] = useState(null);
  const [includeResolved, setIncludeResolved] = useState(false);
  const [expanded, setExpanded] = useState(null); // fingerprint of the open group
  const [occurrences, setOccurrences] = useState({}); // fingerprint -> [records]
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // Self-healing: the prebuilt trigger's state (does it exist? enabled? is automation on?).
  const [selfheal, setSelfheal] = useState(null); // {on, trigger} | null while loading
  const [healBusy, setHealBusy] = useState(false);

  async function loadSelfheal() {
    try {
      const data = await sysGet("/triggers");
      const trig = (data.triggers || []).find((t) => t.name === SELFHEAL_NAME && t.kind === "error_spike");
      setSelfheal({ automation: !!data.config.enabled, trigger: trig || null,
        on: !!(data.config.enabled && trig && trig.enabled) });
    } catch {
      setSelfheal(null);
    }
  }

  // One click: create the self-heal trigger if needed, enable it, and flip the master switch
  // on — or turn it off. Autonomous fixes still hold for approval (see the Automation card).
  async function toggleSelfheal() {
    if (!selfheal || healBusy) return;
    setHealBusy(true);
    try {
      if (!selfheal.on) {
        if (!selfheal.trigger) {
          await sysPost("/triggers", SELFHEAL_SPEC);
        } else if (!selfheal.trigger.enabled) {
          await sysPost("/triggers/toggle", { id: selfheal.trigger.id, enabled: true });
        }
        if (!selfheal.automation) {
          await sysPost("/config", { patch: { "triggers.enabled": true } });
        }
        toast.ok("Self-healing on — error spikes will auto-file a fix for your approval.");
      } else {
        if (selfheal.trigger) await sysPost("/triggers/toggle", { id: selfheal.trigger.id, enabled: false });
        toast.info("Self-healing paused. (Other automation, if any, still runs.)");
      }
      await loadSelfheal();
    } finally {
      setHealBusy(false);
    }
  }

  async function load() {
    setLoading(true);
    setError("");
    try {
      const data = await appGet("/api/errors" + (includeResolved ? "?include_resolved=true" : ""));
      setGroups(data.groups || []);
      setSummary(data.summary || null);
    } catch (e) {
      setError(String(e.message || e));
    }
    setLoading(false);
  }
  useEffect(() => {
    load();
  }, [includeResolved]);
  useEffect(() => {
    loadSelfheal();
  }, []);

  async function toggleExpand(g) {
    const fp = g.fingerprint;
    if (expanded === fp) return setExpanded(null);
    setExpanded(fp);
    // Boot groups carry their crash log inline; app groups fetch full occurrences.
    if (g.source !== "boot" && !occurrences[fp]) {
      try {
        const data = await appGet(`/api/errors/${fp}`);
        setOccurrences((o) => ({ ...o, [fp]: data.occurrences || [] }));
      } catch {
        setOccurrences((o) => ({ ...o, [fp]: [] }));
      }
    }
  }

  async function setResolved(g, resolved) {
    await appPost(`/api/errors/${g.fingerprint}/${resolved ? "resolve" : "unresolve"}`, {});
    load();
  }

  async function clearAll() {
    if (!(await confirm({
      title: "Clear all errors",
      body: "Delete all recorded errors and resolution state? This cannot be undone.",
      danger: true,
    }))) return;
    await appDelete("/api/errors");
    setExpanded(null);
    setOccurrences({});
    load();
  }

  function fixWithAgent(g) {
    localStorage.setItem("quine-selfmod-prompt", fixPrompt(g));
    if (onNavigate) onNavigate("modify");
  }

  return (
    <Card
      title="Errors"
      actions={
        <>
          <Button onClick={() => setIncludeResolved((v) => !v)}>
            {includeResolved ? "Hide resolved" : "Show resolved"}
          </Button>
          <Button onClick={clearAll}>Clear all</Button>
          <Button onClick={load}>Refresh</Button>
        </>
      }
    >
      {summary && summary.groups > 0 && (
        <div className="banner err" style={{ marginBottom: 12 }}>
          {summary.groups} unresolved error group{summary.groups === 1 ? "" : "s"}
          {summary.in_version > 0 && ` — ${summary.in_version} seen in the active version`}
        </div>
      )}
      {selfheal && (
        <div className="selfheal-bar">
          <div>
            <strong>{selfheal.on ? "🩹 Self-healing is on" : "🩹 Self-healing"}</strong>
            <span className="muted">
              {" "}— when an error spikes, the agent auto-files a fix and holds it for your approval
              {selfheal.on && selfheal.trigger?.last_fired
                ? ` (last fired ${ts(selfheal.trigger.last_fired)})`
                : ". Verify a fix, then one click ships it."}
            </span>
          </div>
          <Button variant={selfheal.on ? "ghost" : "primary"} onClick={toggleSelfheal} disabled={healBusy}>
            {healBusy ? "…" : selfheal.on ? "Turn off" : "Turn on self-healing"}
          </Button>
        </div>
      )}
      {error && (
        <div className="banner err" style={{ marginBottom: 12 }}>
          {error}
        </div>
      )}
      {loading ? (
        <Empty>Loading…</Empty>
      ) : groups.length === 0 ? (
        <Empty>
          No errors recorded. Runtime exceptions, tool failures, and boot crashes of rejected
          versions will appear here.
        </Empty>
      ) : (
        <div className="error-groups">
          {groups.map((g) => (
            <div key={g.fingerprint} className="card" style={{ padding: 12, marginBottom: 10 }}>
              <div className="row" style={{ alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <Badge tone={sourceTone(g.source)}>{g.source}</Badge>
                {g.count > 1 && <Badge>×{g.count}</Badge>}
                {g.resolved && <Badge tone="good">resolved</Badge>}
                <strong className="mono">{g.exc_type}</strong>
                <span className="muted" style={{ overflowWrap: "anywhere" }}>
                  {(g.message || "").slice(0, 200)}
                </span>
              </div>
              <div className="row muted" style={{ gap: 12, marginTop: 6, fontSize: "0.85em", flexWrap: "wrap" }}>
                {g.route && <span className="mono">{g.route}</span>}
                {(g.versions || []).length > 0 && (
                  <span className="mono">v: {g.versions.map((v) => v.slice(0, 8)).join(", ")}</span>
                )}
                {g.last_ts && <span>last {ts(g.last_ts)}</span>}
                <span className="mono">{g.fingerprint}</span>
              </div>
              <div className="row" style={{ gap: 8, marginTop: 8 }}>
                <Button onClick={() => toggleExpand(g)}>
                  {expanded === g.fingerprint ? "Hide details" : "Details"}
                </Button>
                {g.source !== "boot" && (
                  <Button onClick={() => setResolved(g, !g.resolved)}>
                    {g.resolved ? "Unresolve" : "Resolve"}
                  </Button>
                )}
                <Button variant="primary" onClick={() => fixWithAgent(g)}>
                  Fix with agent
                </Button>
              </div>
              {expanded === g.fingerprint &&
                (g.source === "boot" ? (
                  <Pre className="mt">{g.last_traceback || "(no crash log captured)"}</Pre>
                ) : occurrences[g.fingerprint] ? (
                  occurrences[g.fingerprint].length === 0 ? (
                    <Empty>Could not load occurrences.</Empty>
                  ) : (
                    occurrences[g.fingerprint].map((r) => (
                      <div key={r.id} className="mt">
                        <div className="muted" style={{ fontSize: "0.85em" }}>
                          {ts(r.ts)} — {r.source}
                          {r.route ? ` ${r.route}` : ""}
                          {r.version ? ` — v ${r.version.slice(0, 8)}` : ""}
                        </div>
                        <Pre>{r.traceback || `${r.exc_type}: ${r.message}`}</Pre>
                      </div>
                    ))
                  )
                ) : (
                  <Empty>Loading occurrences…</Empty>
                ))}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
