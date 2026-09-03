import React, { useEffect, useRef, useState } from "react";
import { sysGet, sysPost, appGet, appPost, apiUrl, beginReconnect } from "../api.js";
import {
  Badge,
  Button,
  Empty,
  HarnessDisclosure,
  HarnessMetric,
  HarnessStatusBar,
  HarnessStatusDot,
  Modal,
  Pre,
  Spinner,
  TextArea,
  TextInput,
  eventTone,
} from "../components";
import { useToast } from "../components/Toast.jsx";
import { useConfirm } from "../components/Confirm.jsx";
import { SELFMOD_TEMPLATES } from "../templates.js";

// Ask the agent to rewrite this app. Progress streams over the kernel's /events SSE AND is
// persisted per-task (status.json + events.jsonl), so a run is fully recoverable after a
// tab switch or full page reload — on mount we replay the latest task, then stream live.

// ── Phase model ──────────────────────────────────────────────────────────────────────
// The self-mod pipeline as a linear set of phases. We derive the current phase PURELY from
// the event `kind`s already streaming over /events (no extra backend data), so the stepper
// gives the user a legible "where are we now" without any new syscalls.
const PHASES = [
  { key: "start", label: "Queued" },
  { key: "editing", label: "Editing" },
  { key: "validating", label: "Validating" },
  { key: "committing", label: "Committing" },
  { key: "rebooting", label: "Rebooting" },
  { key: "verifying", label: "Verifying" },
  { key: "live", label: "Live" },
];
const KIND_PHASE = {
  request: 0, start: 0, queue_resume: 0, restored: 0,
  thought: 1, assistant: 1, tool_call: 1, tool_result: 1, steer_received: 1,
  pre_steer: 1, stdout: 1, end_no_tools: 1, runtime_fallback: 1,
  propose: 2, propose_commit: 2,
  review_start: 2, review_pass: 2, review_issues: 2, review_error: 2,
  // verify_derive fires right after the commit (deriving WHAT to verify); the checks
  // themselves run against the booted candidate, so verify/verify_failed land after
  // the blue-green boot — Verifying sits between Rebooting and Live.
  committed: 3, version_committed: 3, pending: 3, promotion_pending: 3, verify_derive: 3,
  reboot: 4, reboot_begin: 4,
  verify: 5, verify_failed: 5,
  promoted: 6, boot_ok: 6,
  line: 6, line_advanced: 6,  // a line change "goes live" on its preview env
};
const FAIL_KINDS = new Set([
  "rolled_back", "rollback", "health_failed", "boot_health_failed", "boot_failed",
  "engine_error", "error", "worker_error", "monitor_unhealthy", "revert_conflict", "reapply_conflict",
  "verify_failed",
]);

// Fold the event log + final result into { reached: phase index, outcome } for the stepper.
function deriveRun(log, result, running) {
  let reached = 0;
  let failed = false, pending = false, cancelled = false, interrupted = false;
  for (const ev of log) {
    const k = ev.kind;
    if (k in KIND_PHASE) reached = Math.max(reached, KIND_PHASE[k]);
    if (FAIL_KINDS.has(k)) failed = true;
    if (k === "pending" || k === "promotion_pending") pending = true;
    if (k === "cancelled") cancelled = true;
    if (k === "interrupted") interrupted = true;
  }
  if (result) {
    if (result.promoted || result.line_promoted) { reached = 6; failed = false; }
    else if (result.pending || result.state === "pending") pending = true;
    else if (result.cancelled || result.state === "cancelled") cancelled = true;
    else if (result.state === "interrupted") interrupted = true;
  }
  let outcome;
  if (running) outcome = "running";
  else if (result && (result.promoted || result.line_promoted)) outcome = "done";
  else if (pending) outcome = "pending";
  else if (cancelled) outcome = "cancelled";
  else if (interrupted) outcome = "interrupted";
  else if (failed || (result && !result.promoted)) outcome = "failed";
  else outcome = "idle";
  return { reached, outcome };
}

// Which litellm model to switch to when leaving the scripted engine: first real preset, else
// the current model if it's already real, else a sensible keyed default.
function litellmTarget(cfg) {
  const agents = (cfg && cfg.agents) || [];
  const preset = agents.find((a) => a.engine === "litellm" && a.model && a.model !== "scripted");
  if (preset) return preset.model;
  const cur = cfg && cfg.agent && cfg.agent.model;
  if (cur && cur !== "scripted") return cur;
  return "deepseek/deepseek-v4-flash";
}

function fmtElapsed(sec) {
  if (sec == null || sec < 0 || !isFinite(sec)) return "—";
  const s = Math.floor(sec % 60);
  const m = Math.floor(sec / 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

const QUICK_STEERS = [
  "Wrap up and propose_commit now.",
  "Make sure GET /health still returns 200.",
  "You're going the wrong way — reconsider the approach.",
];

function LogRow({ ev }) {
  const { tone, icon } = eventTone(ev.kind);
  if (ev.kind === "thought") {
    return (
      <div className="log-row thought-row">
        <Badge tone={tone}>{icon} thinking</Badge>
        <span className="log-thought">{ev.thought || ev.summary}</span>
      </div>
    );
  }
  if (ev.kind === "tool_call") {
    return (
      <div className="log-row tool">
        <span className="toolchip">
          <span className="toolchip-name">{icon} {ev.name || "tool"}</span>
        </span>
        {ev.args && <code className="log-args">{ev.args}</code>}
      </div>
    );
  }
  return (
    <div className="log-row">
      <Badge tone={tone}>{icon} {ev.kind}</Badge>
      {ev.summary && <span className="log-summary">{ev.summary}</span>}
    </div>
  );
}

function ResultBadge({ result }) {
  const cancelled = result.cancelled || result.state === "cancelled";
  const interrupted = result.state === "interrupted";
  const tone = result.promoted || result.line_promoted ? "good"
    : cancelled || interrupted ? "warn" : "bad";
  const label = result.promoted
    ? "promoted"
    : result.line_promoted
      ? `line '${result.line}' updated`
      : cancelled
        ? "cancelled"
        : interrupted
          ? "interrupted"
          : "not promoted";
  return <Badge tone={tone}>{label}</Badge>;
}

// A single-line badge summarizing a finished run (used in the run-view header meta).
function OutcomeBadge({ outcome, result }) {
  const map = {
    done: ["good", result && result.line_promoted ? "line updated" : "promoted"],
    failed: ["bad", "not promoted"],
    pending: ["warn", "awaiting approval"],
    cancelled: ["warn", "cancelled"],
    interrupted: ["warn", "interrupted"],
    idle: ["muted", "idle"],
  };
  const [tone, label] = map[outcome] || ["muted", outcome];
  return <Badge tone={tone}>{label}</Badge>;
}

// The pipeline phase stepper. `reached` is the furthest phase index seen; `outcome` colors the
// current node (active while running, ✓ done, ✗ on failure, ! when stopped).
function PhaseStepper({ reached, outcome }) {
  return (
    <div className="phase-stepper">
      {PHASES.map((p, i) => {
        let state;
        if (outcome === "done") state = "done";
        else if (i < reached) state = "done";
        else if (i === reached) {
          state = outcome === "failed" ? "failed"
            : outcome === "cancelled" || outcome === "interrupted" ? "stopped"
              : outcome === "running" ? "active"
                : "done"; // pending / idle: this phase completed
        } else state = "pending";
        const mark = state === "done" ? "✓" : state === "failed" ? "✗" : state === "stopped" ? "!" : i + 1;
        return (
          <React.Fragment key={p.key}>
            {i > 0 && <span className={"phase-conn" + (i <= reached ? " done" : "")} />}
            <div className={"phase-node " + state}>
              <span className="phase-dot">{mark}</span>
              <span className="phase-label">{p.label}</span>
            </div>
          </React.Fragment>
        );
      })}
    </div>
  );
}

export default function SelfModifyTab({ onStatus, status }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [prompt, setPrompt] = useState(() => localStorage.getItem("quine-selfmod-prompt") || "");
  const [steer, setSteer] = useState(() => localStorage.getItem("quine-selfmod-steer") || "");
  const [log, setLog] = useState([]); // raw event objects {kind, summary, seq, name, args, thought, t}
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [rawResult, setRawResult] = useState(false);
  const [recovering, setRecovering] = useState(true);
  const [session, setSession] = useState({ active: false, committed: false, messages: 0 });
  const [requireApproval, setRequireApproval] = useState(null); // diff-preview gate; null=unknown
  const [reviewEnabled, setReviewEnabled] = useState(null); // review-agent gate; null=unknown
  const [reviewModel, setReviewModel] = useState(""); // reviewer model ("" = same as agent)
  const [verifierEnabled, setVerifierEnabled] = useState(null); // Verification Gate; null=unknown
  const [checks, setChecks] = useState([]); // the frozen regression suite (state/checks.json)
  const [lines, setLines] = useState([]); // named lines, for the target picker
  const [target, setTarget] = useState(""); // "" = production; else the line to change
  const [newLineOpen, setNewLineOpen] = useState(false);
  const [newLine, setNewLine] = useState("");
  const [creatingLine, setCreatingLine] = useState(false);
  const [cfg, setCfg] = useState(null); // kernel config (for engine + model + presets)
  const [switching, setSwitching] = useState(false); // engine switch in flight
  const [approving, setApproving] = useState(false); // approve/reject in flight
  const [now, setNow] = useState(() => Date.now()); // ticks while running, for elapsed time
  const [convos, setConvos] = useState([]); // archived self-mod conversations
  const [viewing, setViewing] = useState(null); // { id, messages } transcript modal
  const [busyConvo, setBusyConvo] = useState(false);
  // "Continue from a commit": the version the next request re-bases on + resumes. Set from the
  // Versions tab (via a one-shot localStorage intent) or the Previous-conversations list below.
  const [continueFrom, setContinueFrom] = useState(null); // {sha, short, seq, label, task, branch}
  const [versionByTask, setVersionByTask] = useState({}); // task_id → {sha, seq, short, on_main}
  // Advisor (features/advisor.py plugin): improvement proposals mined from the system's own
  // telemetry. advisorUp=false (plugin uninstalled/disabled → 404) hides the panel entirely.
  const [proposals, setProposals] = useState([]);
  const [lastAnalysis, setLastAnalysis] = useState(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [advisorUp, setAdvisorUp] = useState(true);
  const [advisorCfg, setAdvisorCfg] = useState(null); // {auto_analyze_minutes}
  const [schedulingId, setSchedulingId] = useState(null); // proposal with the time picker open
  const [schedTime, setSchedTime] = useState("03:00"); // HH:MM, UTC
  // With operator_auth on, the APP process can't create/delete kernel triggers, so the
  // Schedule…/Cancel-schedule flows (which go through the plugin's server side) can't work.
  const [opAuthOn, setOpAuthOn] = useState(false);
  // Agent evals: the held-out benchmark gating changes to the agent's own runtime.
  const [evalsInfo, setEvalsInfo] = useState(null); // {tasks, enabled, strict, paths}
  const [evalName, setEvalName] = useState("");
  const [evalPrompt, setEvalPrompt] = useState("");
  const [benchRunning, setBenchRunning] = useState(false);
  const logRef = useRef(null);
  const pinnedRef = useRef(true); // is the log scrolled to the bottom?
  const taskRef = useRef(null); // the task id whose events we're currently showing
  const lastSeqRef = useRef(0); // highest seq applied (de-dupes replay vs live stream)

  const engine = cfg && cfg.agent ? cfg.agent.engine : null;
  const engineModel = cfg && cfg.agent ? cfg.agent.model : "";
  const maxSteps = cfg && cfg.agent ? cfg.agent.max_steps : 0;

  // Apply one live event, de-duping against what we already replayed and switching to a
  // newly-started task if one appears.
  function applyEvent(ev) {
    if (!ev || typeof ev.seq !== "number") return;
    if (ev.task && ev.task !== taskRef.current) {
      taskRef.current = ev.task; // a newer task started — reset the view to it
      lastSeqRef.current = ev.seq;
      setResult(null);
      setRunning(true);
      setLog([ev]);
    } else if (ev.seq > lastSeqRef.current) {
      lastSeqRef.current = ev.seq;
      setLog((l) => [...l, ev]);
    } else {
      return; // already have it (from the on-mount replay)
    }
    if (ev.kind === "done") {
      setRunning(false);
      if (ev.result) setResult(ev.result);
      refreshSession();
      if (onStatus) onStatus();
      if (ev.result && ev.result.promoted) {
        setLog((l) => [...l, { kind: "done", seq: ev.seq + 0.5, summary: "promoted — reloading into the new version…" }]);
        beginReconnect("Rebooting into the new version…");
      }
    }
  }

  useEffect(() => {
    let closed = false;
    // 1) Recover the latest task from disk so progress survives tab switches / reloads.
    (async () => {
      try {
        const data = await sysGet("/task");
        if (!closed && data && data.task) {
          const evs = data.events || [];
          taskRef.current = data.task.task_id;
          lastSeqRef.current = evs.reduce((m, e) => Math.max(m, e.seq || 0), 0);
          setLog(evs);
          setRunning(data.task.state === "running");
          setResult(data.task.result || (data.task.state !== "running" ? { state: data.task.state } : null));
        }
      } catch {
        /* no prior task / kernel unreachable */
      }
      if (!closed) setRecovering(false);
    })();
    // 2) Stream live (survives the reboot a successful change triggers).
    const es = new EventSource(apiUrl("/api/syscall/events"));
    es.onmessage = (e) => {
      let ev;
      try {
        ev = JSON.parse(e.data);
      } catch {
        return;
      }
      applyEvent(ev);
    };
    refreshSession();
    return () => {
      closed = true;
      es.close();
    };
  }, []);

  // Tick a clock every second WHILE running so the elapsed timer advances live.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);

  // Autoscroll the log ONLY when already at the bottom — never yank the user down while
  // they're scrolled up reading earlier steps.
  useEffect(() => {
    const el = logRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [log]);

  function onLogScroll() {
    const el = logRef.current;
    if (el) pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }

  // Persist form text across tab switches
  useEffect(() => {
    localStorage.setItem("quine-selfmod-prompt", prompt);
  }, [prompt]);
  useEffect(() => {
    localStorage.setItem("quine-selfmod-steer", steer);
  }, [steer]);

  // Load kernel config: the engine (scripted vs litellm) + model + presets for the engine strip,
  // and the diff-preview gate (agent.require_approval).
  useEffect(() => {
    (async () => {
      try {
        const { config } = await sysGet("/config");
        setCfg(config || null);
        setRequireApproval(!!(config && config.agent && config.agent.require_approval));
        setReviewEnabled(!!(config && config.agent && config.agent.review_enabled));
        setReviewModel((config && config.agent && config.agent.review_model) || "");
        setVerifierEnabled(!!(config && config.verifier && config.verifier.enabled));
      } catch {
        /* kernel unreachable */
      }
    })();
  }, []);

  // The frozen regression suite: load on mount and re-load after every finished run (a
  // promoted change may have frozen new checks; a revert/rollback may have retired some).
  async function loadChecks() {
    try {
      const data = await sysGet("/checks");
      setChecks(data.checks || []);
    } catch {
      /* kernel unreachable */
    }
  }
  async function loadEvals() {
    try {
      setEvalsInfo(await sysGet("/evals"));
    } catch {
      /* kernel unreachable */
    }
  }
  async function loadLines() {
    try {
      const data = await sysGet("/lines");
      const rows = data.lines || [];
      setLines(rows);
      // The selected line may have been deleted elsewhere — fall back to production.
      setTarget((t) => (t && !rows.some((l) => l.name === t) ? "" : t));
    } catch {
      /* kernel unreachable */
    }
  }
  async function loadProposals() {
    try {
      const data = await appGet("/api/plugins/advisor/proposals");
      setProposals(data.proposals || []);
      setLastAnalysis(data.last_analysis || null);
      setAdvisorCfg(data.config || null);
      setAdvisorUp(true);
    } catch {
      setAdvisorUp(false); // plugin unavailable — keep the tab clean
    }
  }
  async function loadOperatorStatus() {
    try {
      const data = await sysGet("/operator/status");
      setOpAuthOn(!!(data && data.enabled));
    } catch {
      /* kernel unreachable */
    }
  }
  useEffect(() => {
    if (!running) {
      loadChecks();
      loadLines();
      loadProposals(); // a finished run may have consumed or outdated a proposal
      loadEvals();
      loadOperatorStatus();
    }
  }, [running]);

  // ── Agent evals actions ─────────────────────────────────────────────────────────────
  async function toggleEvalsGate() {
    const next = !(evalsInfo && evalsInfo.enabled);
    const { status: st, data } = await sysPost("/config", { patch: { "evals.enabled": next } });
    if (st === 200 && data && data.ok) loadEvals();
    else toast.err("Couldn't toggle evals: " + (((data && data.errors) || []).join("; ") || "?"));
  }

  async function addEvalTask() {
    const name = evalName.trim();
    const prompt = evalPrompt.trim();
    if (!name || !prompt) return;
    const { status: st, data } = await sysPost("/evals", { name, prompt });
    if (st === 200 && data && data.ok) {
      setEvalName("");
      setEvalPrompt("");
      loadEvals();
    } else {
      toast.err("Add failed: " + ((data && data.reason) || "?"));
    }
  }

  async function toggleEvalTask(t) {
    await sysPost("/evals/toggle", { id: t.id, enabled: !(t.enabled !== false) });
    loadEvals();
  }

  async function deleteEvalTask(t) {
    const ok = await confirm({
      title: "Delete benchmark task", body: `Delete "${t.name}"?`,
      danger: true, confirmLabel: "Delete",
    });
    if (!ok) return;
    await sysPost("/evals/delete", { id: t.id });
    loadEvals();
  }

  async function benchmarkNow() {
    if (benchRunning) return;
    setBenchRunning(true);
    try {
      const { status: st, data } = await sysPost("/evals/run", {});
      if (st === 200 && data && data.ok) {
        const r = data.report;
        (r.ok ? toast.ok : toast.warn)(
          `Benchmark of ${data.short}: ${r.passed}/${r.total} task${r.total === 1 ? "" : "s"} passed.`);
      } else {
        toast.err("Benchmark failed: " + ((data && data.reason) || "?"));
      }
    } catch {
      toast.err("Benchmark failed: kernel unreachable.");
    }
    setBenchRunning(false);
    loadEvals();
  }

  // ── Advisor actions ────────────────────────────────────────────────────────────────
  async function analyzeNow() {
    if (analyzing) return;
    setAnalyzing(true);
    try {
      const r = await appPost("/api/plugins/advisor/analyze", {});
      if (r.ok) {
        toast.ok(r.new
          ? `Advisor found ${r.new} new suggestion${r.new === 1 ? "" : "s"}.`
          : "Analysis done — nothing new to suggest." + (r.reason ? ` (${r.reason})` : ""));
      } else {
        toast.err("Analysis failed: " + (r.reason || "?"));
      }
    } catch {
      toast.err("Analysis failed: advisor unreachable.");
    }
    setAnalyzing(false);
    loadProposals();
  }

  // Mirror of the backend's run_prompt(): what a proposal submits / drops into the composer.
  function proposalPrompt(p) {
    let text = `[advisor:${p.id}] ${p.title}\n\n${p.prompt}`;
    if (p.evidence && p.evidence.length) {
      text += "\n\nEvidence (from the system's own telemetry):\n" +
        p.evidence.map((ev) => `- ${ev.kind}: ${ev.ref} — ${ev.summary}`).join("\n");
    }
    if (p.acceptance_criteria && p.acceptance_criteria.length) {
      text += "\n\nAcceptance criteria:\n" + p.acceptance_criteria.map((c) => "- " + c).join("\n");
    }
    return text;
  }

  // "Edit first": drop the proposal's prompt into the composer to steer before submitting.
  function useProposal(p) {
    setPrompt(proposalPrompt(p));
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // "Run": enqueue the proposal as an ordinary change_request via the plugin (which marks it
  // run). Same fire-and-forget contract as the composer — live log + result arrive over SSE.
  async function runProposal(p) {
    if (running) return;
    if (engine === "scripted") {
      const go = await confirm({
        title: "You're on the scripted demo engine",
        body: (
          <>
            The <strong>scripted</strong> engine is a keyless offline demo — it{" "}
            <strong>ignores the proposal</strong> and only makes a placeholder change. Switch to
            a real model to actually implement it.
          </>
        ),
        confirmLabel: "Run demo anyway",
        cancelLabel: "Cancel",
      });
      if (!go) return;
    }
    setRunning(true);
    setResult(null);
    setLog([]);
    lastSeqRef.current = 0;
    taskRef.current = null;
    setProposals((ps) => ps.map((x) => (x.id === p.id ? { ...x, status: "run" } : x)));
    appPost("/api/plugins/advisor/run", { id: p.id })
      .then((r) => {
        // Refused before it ever queued (e.g. another task holds the lock) → surface + reset.
        if (!r.ok && !(r.result && r.result.task)) {
          setRunning(false);
          toast.err("Run failed: " + ((r.result && (r.result.reason || r.result.error)) || r.reason || "?"));
        }
        loadProposals();
      })
      .catch(() => loadProposals());
  }

  async function dismissProposal(p) {
    const r = await appPost("/api/plugins/advisor/dismiss", { id: p.id });
    if (!r.ok) toast.err("Dismiss failed: " + (r.reason || "?"));
    loadProposals();
  }

  async function setAutoAnalyze(minutes) {
    const r = await appPost("/api/plugins/advisor/config", { auto_analyze_minutes: minutes });
    if (r.ok) setAdvisorCfg(r.config);
    else toast.err("Couldn't change auto-analyze: " + (r.reason || "?"));
  }

  // "Schedule…": run the proposal ONCE at HH:MM UTC via a one-shot kernel automation, so the
  // unattended run rides the trigger rails (master switch, daily cap, hold-unless-verified).
  async function scheduleProposal(p) {
    const t = schedTime.trim();
    const r = await appPost("/api/plugins/advisor/schedule", { id: p.id, daily_at: t });
    if (r.ok) {
      setSchedulingId(null);
      if (r.triggers_enabled === false) {
        toast.warn(
          `Scheduled for ${t} UTC — but the Automation master switch is OFF ` +
          "(Settings ▸ Automation), so it won't fire until you enable it.");
      } else {
        toast.ok(`Scheduled for ${t} UTC. It runs once, under your automation approval rules.`);
      }
    } else {
      toast.err("Schedule failed: " + (r.reason || "?"));
    }
    loadProposals();
  }

  async function unscheduleProposal(p) {
    const r = await appPost("/api/plugins/advisor/unschedule", { id: p.id });
    if (!r.ok) toast.err("Cancel schedule failed: " + (r.reason || "?"));
    loadProposals();
  }

  async function createLine() {
    const name = newLine.trim().toLowerCase();
    if (!name || creatingLine) return;
    setCreatingLine(true);
    const { status: st, data } = await sysPost("/line", { name });
    setCreatingLine(false);
    if (st === 200 && data && data.ok) {
      setNewLine("");
      setNewLineOpen(false);
      setTarget(name);
      await loadLines();
      const pv = data.preview || {};
      toast.ok(pv.ok
        ? `Line '${name}' created — preview at ${data.url}`
        : `Line '${name}' created (preview not started: ${pv.reason || "?"})`);
    } else {
      toast.err("Create line failed: " + ((data && data.reason) || "?"));
    }
  }

  async function toggleCheck(c) {
    const { status: st, data } = await sysPost("/checks/toggle", {
      id: c.id, enabled: c.status !== "active",
    });
    if (st === 200 && data && data.ok) loadChecks();
    else toast.err("Check toggle failed: " + ((data && data.reason) || "?"));
  }

  // Pick up a "continue from a commit" intent handed off from the Versions tab (one-shot), and
  // load the version list so the Previous-conversations card can map each convo's task → its
  // version ("Continue from vN").
  useEffect(() => {
    try {
      const raw = localStorage.getItem("quine-continue-intent");
      if (raw) {
        localStorage.removeItem("quine-continue-intent"); // consume it — a refresh shouldn't re-arm
        const intent = JSON.parse(raw);
        if (intent && intent.sha) {
          setContinueFrom(intent);
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      }
    } catch {
      /* ignore */
    }
    (async () => {
      try {
        const data = await sysGet("/versions?limit=500");
        const map = {};
        for (const v of data.versions || []) {
          if (v.task) map[v.task] = { sha: v.sha, seq: v.seq, short: v.short, on_main: v.on_main };
        }
        setVersionByTask(map);
      } catch {
        /* kernel unreachable */
      }
    })();
  }, []);

  async function toggleApproval() {
    const next = !requireApproval;
    setRequireApproval(next); // optimistic
    const { status: st, data } = await sysPost("/config", {
      patch: { "agent.require_approval": next },
    });
    if (st === 200 && data && data.ok) setCfg(data.config);
    else setRequireApproval(!next); // revert on failure
  }

  async function toggleReview() {
    const next = !reviewEnabled;
    setReviewEnabled(next); // optimistic
    const { status: st, data } = await sysPost("/config", {
      patch: { "agent.review_enabled": next },
    });
    if (st === 200 && data && data.ok) setCfg(data.config);
    else setReviewEnabled(!next); // revert on failure
  }

  async function toggleVerifier() {
    const next = !verifierEnabled;
    setVerifierEnabled(next); // optimistic
    const { status: st, data } = await sysPost("/config", {
      patch: { "verifier.enabled": next },
    });
    if (st === 200 && data && data.ok) setCfg(data.config);
    else setVerifierEnabled(!next); // revert on failure
  }

  async function changeReviewModel(model) {
    const prev = reviewModel;
    setReviewModel(model); // optimistic
    const { status: st, data } = await sysPost("/config", {
      patch: { "agent.review_model": model },
    });
    if (st === 200 && data && data.ok) setCfg(data.config);
    else setReviewModel(prev); // revert on failure
  }

  // One-click: leave the keyless scripted demo and start using a real model. Patches BOTH engine
  // AND model (scripted's model is the literal "scripted", which litellm can't route). Live —
  // takes effect on the next request, no reboot.
  async function switchToLitellm() {
    if (switching) return;
    const model = litellmTarget(cfg);
    setSwitching(true);
    const { status: st, data } = await sysPost("/config", {
      patch: { "agent.engine": "litellm", "agent.model": model },
    });
    setSwitching(false);
    if (st === 200 && data && data.ok) {
      setCfg(data.config);
      toast.ok(`Now using ${model}. If a run fails with an auth error, add a provider key in Settings.`);
    } else {
      toast.err("Couldn't switch engine: " + (((data && data.errors) || []).join("; ") || "unknown error"));
    }
  }

  // Fill the request box from a template (the user reviews, then submits).
  function useTemplate(t) {
    setPrompt(t.prompt);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // Kick off a change request: reset the run view and fire-and-forget — the live log + final
  // result arrive over the SSE stream (and are persisted), so this can outlive the page.
  // `opts` carries an optional "continue from a commit" re-base (base_version + resume_task).
  function beginChangeRequest(text, opts) {
    setRunning(true);
    setResult(null);
    setLog([]);
    lastSeqRef.current = 0;
    taskRef.current = null;
    const body = { prompt: text };
    if (opts && opts.sha) {
      body.base_version = opts.sha;
      if (opts.task) body.resume_task = opts.task;
    } else if (target) {
      body.line = target; // line target: the change lands on the line's preview, not prod
    }
    sysPost("/change_request", body).catch(() => {});
  }

  async function submit() {
    const p = prompt.trim();
    if (!p || running) return;
    // Guard: on the scripted demo engine the request is IGNORED (placeholder change only) — warn
    // before wasting a run, and point at the one-click switch above.
    if (engine === "scripted") {
      const go = await confirm({
        title: "You're on the scripted demo engine",
        body: (
          <>
            The <strong>scripted</strong> engine is a keyless offline demo — it{" "}
            <strong>ignores your request</strong> and only makes a placeholder change (it renames
            the build label). Switch to a real model to actually build what you describe.
          </>
        ),
        confirmLabel: "Run demo anyway",
        cancelLabel: "Cancel",
      });
      if (!go) return;
    }
    setPrompt("");
    const opts = continueFrom;
    setContinueFrom(null); // one-shot: the next request goes back to editing the live version
    beginChangeRequest(p, opts);
  }

  async function cancel() {
    await sysPost("/cancel", {});
  }

  async function dequeue(taskId) {
    const { data } = await sysPost("/dequeue", { task_id: taskId });
    if (data.ok) toast.info("Removed from queue.");
    else toast.err("Dequeue failed: " + (data.reason || "?"));
    if (onStatus) onStatus();
  }

  async function refreshSession() {
    try {
      setSession(await appGet("/api/agent/selfmod-session"));
    } catch {
      /* ignore */
    }
    loadConvos();
  }

  async function loadConvos() {
    try {
      const { conversations } = await appGet("/api/agent/selfmod-conversations");
      setConvos(conversations || []);
    } catch {
      /* ignore */
    }
  }

  async function viewConvo(id) {
    try {
      setViewing(await appGet("/api/agent/selfmod-conversations/" + id));
    } catch {
      /* ignore */
    }
  }

  // "Improve a task": drop a past prompt into the composer to tweak and run fresh.
  function reusePrompt(text) {
    setPrompt(text || "");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // "Continue from vN": arm a re-based continue on the version this conversation produced. We're
  // already in this tab, so just set the intent + scroll up to the composer (the actual run is
  // fired on submit with base_version + resume_task).
  function continueFromConvo(c, ver) {
    const isHead = !!(status && status.active && status.active.version === ver.sha);
    setContinueFrom({
      sha: ver.sha, short: ver.short, seq: ver.seq, label: null,
      task: c.task, branch: !isHead,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // "Reuse a cancelled one": reopen a past conversation as the active one so the next
  // request continues it with full context (the current active convo is archived first).
  async function resumeConvo(c) {
    setBusyConvo(true);
    try {
      const r = await appPost("/api/agent/selfmod-conversations/" + c.id + "/resume", {});
      if (r.ok) {
        setResult(null);
        setLog([]);
        lastSeqRef.current = 0;
        taskRef.current = null;
        await refreshSession();
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    } finally {
      setBusyConvo(false);
    }
  }

  // Retire the current (uncommitted) conversation and start clean. After a successful
  // commit the next request is fresh automatically — this is the manual escape hatch.
  async function newConversation() {
    try {
      await appPost("/api/agent/selfmod-session/reset", {});
    } catch {
      /* ignore */
    }
    setResult(null);
    setLog([]);
    lastSeqRef.current = 0;
    taskRef.current = null;
    refreshSession();
  }

  async function sendSteerText(m) {
    const t = (m == null ? "" : m).trim();
    if (!t || !running) return;
    await sysPost("/steer", { message: t });
  }

  async function sendSteer() {
    const m = steer.trim();
    if (!m || !running) return;
    setSteer("");
    await sendSteerText(m);
  }

  // ── Approval (inline; source diffs stay operator-only) ──────────────────────────────
  const pendingSha = result && (result.version || result.short) ? result.version || result.short : null;

  async function approvePending() {
    if (!pendingSha || approving) return;
    setApproving(true);
    const { data } = await sysPost("/approve", { sha: pendingSha });
    setApproving(false);
    if (data && data.ok && data.promoted) {
      beginReconnect("Rebooting into the approved version…");
    } else if (data && data.ok) {
      toast.warn("Approved, but the health check failed: " + (data.reason || "?"));
      refreshSession();
      if (onStatus) onStatus();
    } else {
      toast.err("Approve failed: " + ((data && data.reason) || "?"));
    }
  }

  async function rejectPending() {
    if (!pendingSha || approving) return;
    setApproving(true);
    const { data } = await sysPost("/reject", { sha: pendingSha });
    setApproving(false);
    if (data && data.ok) {
      toast.info("Change rejected — the app stays on the current version.");
      setResult(null);
      setLog([]);
      lastSeqRef.current = 0;
      taskRef.current = null;
    } else {
      toast.err("Reject failed: " + ((data && data.reason) || "?"));
    }
    refreshSession();
    if (onStatus) onStatus();
  }

  const { reached, outcome } = deriveRun(log, result, running);
  const showRun = running || log.length > 0 || !!result;
  const stepCount = log.reduce((n, e) => n + (e.kind === "tool_call" ? 1 : 0), 0);
  const firstT = (log.find((e) => e.t) || {}).t;
  const lastT = ([...log].reverse().find((e) => e.t) || {}).t;
  const elapsed = firstT ? (running ? now / 1000 : (lastT || firstT)) - firstT : null;
  const currentPhase = PHASES[Math.min(reached, PHASES.length - 1)]?.label || "Queued";
  const targetLabel = continueFrom
    ? `v${continueFrom.seq} · ${continueFrom.short}`
    : target
      ? `line:${target}`
      : "production";
  const harnessState = outcome === "running" ? "running"
    : outcome === "done" ? "success"
      : outcome === "pending" ? "pending"
        : outcome === "failed" ? "failed"
          : outcome === "cancelled" || outcome === "interrupted" ? "stopped"
            : "idle";
  const harnessSummary = outcome === "running"
    ? `${currentPhase} the candidate · activity is streaming live`
    : outcome === "pending"
      ? "Candidate is held at the approval gate"
      : outcome === "done"
        ? "Candidate passed the pipeline and is live"
        : outcome === "failed"
          ? "The candidate did not pass the pipeline"
          : "Stage a request, observe every step, and ship only after the gates pass";

  return (
    <div className={"selfmod-console harness-console" + (showRun ? " has-run" : "")}>
      <HarnessStatusBar
        state={harnessState}
        eyebrow="Self-Modify agent · guarded pipeline"
        title={running ? `Executing: ${prompt.trim().slice(0, 72) || "change request"}` : "Self-modification workspace"}
        summary={harnessSummary}
        metrics={
          <>
            <HarnessMetric label="phase" value={currentPhase} />
            <HarnessMetric label="target" value={targetLabel} mono title={targetLabel} />
            <HarnessMetric label="engine" value={engine === "scripted" ? "scripted demo" : (engineModel || "litellm")} title={engineModel} />
            <HarnessMetric label="elapsed" value={fmtElapsed(elapsed)} mono />
            <HarnessMetric label="tools" value={maxSteps ? `${stepCount}/${maxSteps}` : stepCount} mono />
          </>
        }
        actions={running ? <Button variant="danger" onClick={cancel}>Cancel run</Button> : null}
      >
        <div className="phase-stepper-scroll">
          <PhaseStepper reached={reached} outcome={outcome} />
        </div>
      </HarnessStatusBar>

      {/* Engine status + scripted warning */}
      {engine && (
        <section className="engine-card selfmod-engine harness-panel">
          <div className="harness-panel-head">
            <span className="harness-panel-kicker">Runtime</span>
            <h2>Agent engine</h2>
          </div>
          <div className="engine-strip">
            <span className={"engine-chip" + (engine === "scripted" ? " scripted" : "")}>
              <span className="dot" />
              {engine === "scripted" ? "Scripted engine" : "litellm"}
              <span className="engine-model">{engine === "scripted" ? "offline demo" : engineModel}</span>
            </span>
            {engine !== "scripted" && (
              <span className="field-hint engine-note">A real model reads your request and writes real code.</span>
            )}
          </div>
          {engine === "scripted" && (
            <div className="engine-warn">
              <span className="engine-warn-icon" aria-hidden="true">⚠️</span>
              <div className="engine-warn-body">
                <h3>You're on the scripted (offline demo) engine</h3>
                <p>
                  It's a keyless engine for exercising the pipeline offline — it{" "}
                  <strong>ignores your request</strong> and only makes a placeholder change (it
                  renames the build label). Switch to a real model to actually build what you type.
                  litellm needs a provider key — set one in <strong>Settings</strong> (or{" "}
                  <code>state/secrets.env</code>).
                </p>
                <div className="engine-warn-actions">
                  <Button variant="primary" onClick={switchToLitellm} disabled={switching}>
                    {switching ? "Switching…" : "Switch to a real model"}
                  </Button>
                  <span className="muted">→ {litellmTarget(cfg)}</span>
                </div>
              </div>
            </div>
          )}
        </section>
      )}

      {/* Advisor: the observe→propose half of the self-improving loop. Proposals are text
          until a human acts — Run enqueues through the normal pipeline, Edit-first steers. */}
      {advisorUp && (() => {
        const open = proposals.filter((p) => p.status === "open");
        const visible = proposals.filter((p) => p.status === "open" || p.status === "scheduled");
        const effortTone = { small: "good", medium: "accent", large: "warn" };
        return (
          <HarnessDisclosure
            className="advisor-card selfmod-secondary"
            title="Advisor suggestions"
            description="Telemetry-derived missions for the agent"
            badge={<Badge tone={open.length ? "accent" : "muted"}>{open.length} open</Badge>}
            attention={open.length > 0}
          >
            <div className="harness-disclosure-toolbar">
              <select
                className="select"
                value={String((advisorCfg && advisorCfg.auto_analyze_minutes) || 0)}
                onChange={(e) => setAutoAnalyze(Number(e.target.value))}
                title="Mine the telemetry on a clock (produces suggestions only — never runs one)"
              >
                <option value="0">Auto-analyze: off</option>
                <option value="360">Auto-analyze: 6h</option>
                <option value="720">Auto-analyze: 12h</option>
                <option value="1440">Auto-analyze: daily</option>
              </select>
              <Button onClick={analyzeNow} disabled={analyzing}>
                {analyzing ? "Analyzing…" : "⟳ Analyze now"}
              </Button>
            </div>
            <p className="muted" style={{ marginTop: 0 }}>
              The Advisor mines this system's own telemetry — unresolved errors, failed
              versions, the audit trail, model spend — into ready-to-run change requests.
              Suggestions are only text until you act on one.
              {engine === "scripted" && (
                <>
                  {" "}
                  <strong>
                    (The scripted demo engine can't analyze telemetry with a model — it only
                    parses literal <code>__ADVISOR_PROPOSAL__ {"{…}"}</code> markers.)
                  </strong>
                </>
              )}
            </p>
            {visible.length === 0 ? (
              <Empty>
                No suggestions right now — hit <strong>Analyze now</strong> to mine the current
                telemetry.
              </Empty>
            ) : (
              <div className="convo-list">
                {visible.map((p) => (
                  <div key={p.id} className="convo-row advisor-row">
                    <div className="convo-main">
                      <span className="convo-prompt">
                        <Badge tone={effortTone[p.effort] || "accent"}>{p.effort}</Badge>{" "}
                        {p.status === "scheduled" && (
                          <Badge tone="accent" title="Runs once via a one-shot automation; it holds for approval per your automation settings">
                            ⏱ {p.scheduled_for} UTC
                          </Badge>
                        )}{" "}
                        <strong>{p.title}</strong>
                      </span>
                      {p.rationale && <span className="muted">{p.rationale}</span>}
                      <span className="convo-meta muted">
                        {(p.evidence || []).map((ev, i) => (
                          <Badge key={i} tone="muted" title={ev.summary}>
                            {ev.kind}: {ev.ref}
                          </Badge>
                        ))}{" "}
                        {p.created ? new Date(p.created * 1000).toLocaleString() : ""}
                      </span>
                    </div>
                    <div className="row convo-actions">
                      {p.status === "scheduled" ? (
                        <Button onClick={() => unscheduleProposal(p)}
                          title={opAuthOn
                            ? "Operator authorization is on — the app can't delete kernel automations, so this may be refused. An operator can remove the one-shot entry in Settings ▸ Automation."
                            : undefined}>
                          Cancel schedule
                        </Button>
                      ) : schedulingId === p.id ? (
                        <>
                          <TextInput
                            value={schedTime}
                            onChange={(e) => setSchedTime(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && scheduleProposal(p)}
                            placeholder="03:00"
                            maxLength={5}
                            style={{ width: 72 }}
                          />
                          <span className="muted">UTC</span>
                          <Button variant="primary" onClick={() => scheduleProposal(p)}>
                            Schedule
                          </Button>
                          <Button variant="ghost" onClick={() => setSchedulingId(null)}>
                            Cancel
                          </Button>
                        </>
                      ) : (
                        <>
                          <Button onClick={() => dismissProposal(p)} title="Hide this suggestion — it won't be re-proposed for a while">
                            Dismiss
                          </Button>
                          <Button onClick={() => useProposal(p)} title="Load the proposal into the request box to edit before submitting">
                            Edit first
                          </Button>
                          <Button onClick={() => setSchedulingId(p.id)} disabled={opAuthOn}
                            title={opAuthOn
                              ? "Operator authorization is on — the app process can't create kernel automations. Run the proposal directly, or have an operator schedule it in Settings ▸ Automation."
                              : "Run it once at a set time (UTC) via a one-shot automation — held for approval per your automation settings"}>
                            Schedule…
                          </Button>
                          <Button variant="primary" onClick={() => runProposal(p)} disabled={running}>
                            Run
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {lastAnalysis && (
              <span className="field-hint">
                Last analysis: {lastAnalysis.ts ? new Date(lastAnalysis.ts * 1000).toLocaleString() : "—"}
                {lastAnalysis.ok
                  ? ` — ${lastAnalysis.new || 0} new`
                  : ` — failed: ${lastAnalysis.reason || "?"}`}
              </span>
            )}
          </HarnessDisclosure>
        );
      })()}

      {/* Mission brief + controls */}
      <section className="selfmod-mission harness-panel">
        <div className="harness-panel-head">
          <span className="harness-panel-kicker">Mission brief</span>
          <h2>Change this app</h2>
          <p>Describe the outcome. The harness stages, validates, boots, verifies, and can roll it back.</p>
        </div>
        {continueFrom && (
          <div className="banner info continue-banner">
            <div>
              <strong>
                ✎ Continuing from v{continueFrom.seq} · <code>{continueFrom.short}</code>
                {continueFrom.label ? ` (${continueFrom.label})` : ""}
              </strong>
              <p style={{ margin: "4px 0 0" }}>
                The agent will edit from <strong>this version's code</strong>
                {continueFrom.task ? " and resume its original conversation" : ""}. Describe the fix
                or improvement below.
                {continueFrom.branch && (
                  <>
                    {" "}
                    <span className="warn">
                      This isn't the running version, so the new change branches from it — versions
                      after it will leave the active line when it goes live.
                    </span>
                  </>
                )}
              </p>
            </div>
            <Button variant="ghost" onClick={() => setContinueFrom(null)} disabled={running}>
              Edit live version instead
            </Button>
          </div>
        )}
        <TextArea
          rows={4}
          value={prompt}
          disabled={running}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="e.g. Add a Plugins tab that browses and installs Anthropic/Codex plugins"
        />
        {/* Target picker: production (default) or a named line's preview environment. A
            "continue from a commit" already pins its own base, so the picker yields. */}
        {!continueFrom && (
          <div className="row mt target-row">
            <label className="field-label" htmlFor="selfmod-target">Target</label>
            <select
              id="selfmod-target"
              className="select"
              value={target}
              disabled={running}
              onChange={(e) => setTarget(e.target.value)}
            >
              <option value="">Production (goes live on approval/promotion)</option>
              {lines.map((l) => (
                <option key={l.name} value={l.name}>
                  line: {l.name} ({l.short}{l.ahead ? `, +${l.ahead}` : ""})
                </option>
              ))}
            </select>
            {newLineOpen ? (
              <>
                <TextInput
                  value={newLine}
                  onChange={(e) => setNewLine(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && createLine()}
                  placeholder="experiment-1"
                  maxLength={24}
                />
                <Button variant="primary" onClick={createLine} disabled={!newLine.trim() || creatingLine}>
                  {creatingLine ? "Creating…" : "Create line"}
                </Button>
                <Button variant="ghost" onClick={() => { setNewLineOpen(false); setNewLine(""); }}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button variant="ghost" onClick={() => setNewLineOpen(true)} disabled={running}
                title="A named line is a parallel version line with its own preview URL — experiment without touching production">
                ⑂ New line
              </Button>
            )}
          </div>
        )}
        {target && !continueFrom && (
          <span className="field-hint">
            Changes land on <strong>line '{target}'</strong> and its preview at{" "}
            <a href={apiUrl(`/preview/${target}`)} target="_blank" rel="noreferrer">
              /preview/{target}
            </a>{" "}
            — production is untouched until you promote the line (Versions tab).
          </span>
        )}
        <div className="row mt">
          {!running && (
            <Button variant="primary" onClick={submit} disabled={!prompt.trim()}>
              {target ? `Submit to line '${target}'` : "Submit request"}
            </Button>
          )}
          {running && (
            <Button variant="danger" onClick={cancel}>
              Cancel
            </Button>
          )}
          {running && <Spinner label="self-modifying…" />}
        </div>
        <details className="banner info how-selfmod">
          <summary>How self-modification works</summary>
          <p>
            Your request → the agent edits a staging copy of the app → it's validated (syntax,
            imports, the app's own tests, the frontend build) → the app reboots into the new
            version, with an automatic rollback if anything fails its health check — and, with the
            Verification Gate on, only promotes if it passes acceptance checks derived from your
            request plus every frozen regression check. Every change is a git version you can
            revert.
          </p>
        </details>
        <div className="selfmod-gates">
          <div className="selfmod-gates-head">
            <span className="harness-panel-kicker">Safety gates</span>
            <Badge tone={requireApproval && verifierEnabled ? "good" : "warn"}>
              {[requireApproval, reviewEnabled, verifierEnabled].filter(Boolean).length}/3 enabled
            </Badge>
          </div>
        <label className="check approval-toggle">
          <input
            type="checkbox"
            checked={!!requireApproval}
            disabled={requireApproval === null}
            onChange={toggleApproval}
          />{" "}
          Ask my approval before a change goes live
        </label>
        {requireApproval && (
          <span className="field-hint">
            On: each change is held as a <strong>pending version</strong> you approve or reject
            (below, or in the <strong>Versions</strong> tab) before it reboots.
          </span>
        )}
        <label className="check review-toggle">
          <input
            type="checkbox"
            checked={!!reviewEnabled}
            disabled={reviewEnabled === null}
            onChange={toggleReview}
          />{" "}
          Have a second AI review each change first
        </label>
        {reviewEnabled && (
          <span className="field-hint">
            On: after your change passes validation, a second{" "}
            <strong>review agent</strong> inspects the diff for bugs and unmet requirements and
            hands any findings back to the agent to fix before it commits.
            {engine === "scripted" && (
              <> {" "}<strong>(No effect on the offline scripted engine — switch to a real model above.)</strong></>
            )}
          </span>
        )}
        <label className="check verify-toggle">
          <input
            type="checkbox"
            checked={!!verifierEnabled}
            disabled={verifierEnabled === null}
            onChange={toggleVerifier}
          />{" "}
          Verify each change does what was asked (Verification Gate)
        </label>
        {verifierEnabled && (
          <span className="field-hint">
            On: after a change boots healthy, the kernel derives <strong>acceptance
            checks</strong> from your request and runs them — plus every frozen check below —
            against the candidate. It only goes live if it <em>behaves</em>, not just boots;
            passing checks are frozen as permanent regression checks.
            {engine === "scripted" && (
              <> {" "}<strong>(The scripted demo engine can't derive checks from prose — switch
              to a real model, or embed a literal <code>__VERIFY_CHECK__ {"{…}"}</code> marker.)</strong></>
            )}
          </span>
        )}
        {reviewEnabled && engine !== "scripted" && (
          <label className="field review-model-row">
            <span className="field-label">Reviewer model</span>
            <select
              className="select"
              value={reviewModel}
              onChange={(e) => changeReviewModel(e.target.value)}
            >
              <option value="">Same as agent{engineModel ? ` (${engineModel})` : ""}</option>
              {(cfg && Array.isArray(cfg.agents) ? cfg.agents : [])
                .filter((a) => a.engine !== "scripted")
                .map((a) => (
                  <option key={a.model} value={a.model}>
                    {a.name} — {a.model}
                  </option>
                ))}
            </select>
          </label>
        )}
        </div>
        {!running && (
          <div className="row mt session-row">
            {session.active && session.messages > 0 ? (
              <>
                <Badge tone="accent">continuing {session.messages} msgs</Badge>
                <span className="field-hint">
                  nudges keep this context; it resets to fresh after a commit
                </span>
                <Button variant="ghost" onClick={newConversation}>New conversation</Button>
              </>
            ) : (
              <Badge tone="muted">fresh conversation</Badge>
            )}
          </div>
        )}
      </section>

      {/* Live execution stage: log + steering + result/approval */}
      {showRun && (
        <section className="selfmod-execution harness-panel">
          <div className="harness-panel-head selfmod-execution-head">
            <span className="harness-panel-kicker">Execution trace</span>
            <h2>Live agent activity</h2>
            <div className="selfmod-execution-state">
              {running ? <Spinner label={currentPhase.toLowerCase() + "…"} /> : <OutcomeBadge outcome={outcome} result={result} />}
            </div>
          </div>

          <div className="agent-log" ref={logRef} onScroll={onLogScroll}>
            {log.length === 0 && <Empty>{recovering ? "Loading…" : "Agent activity will stream here."}</Empty>}
            {log.map((ev, i) => (
              <LogRow key={ev.seq ?? i} ev={ev} />
            ))}
          </div>

          {running && (
            <div className="steer-bar">
              <div className="steer-quick">
                {QUICK_STEERS.map((q) => (
                  <button key={q} type="button" className="steer-chip" onClick={() => sendSteerText(q)}>
                    {q}
                  </button>
                ))}
              </div>
              <div className="row">
                <TextInput
                  value={steer}
                  onChange={(e) => setSteer(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && sendSteer()}
                  placeholder="Steer the agent — e.g. focus on the Run tab; keep /health working"
                />
                <Button onClick={sendSteer} disabled={!steer.trim()}>
                  Send
                </Button>
              </div>
              <p className="field-hint">Delivered as a high-priority instruction; applied on the agent's next step.</p>
            </div>
          )}

          {/* Inline approval (source diffs are operator-only, so this leans on the log + message). */}
          {!running && outcome === "pending" && (
            <div className="engine-warn approval-panel">
              <span className="engine-warn-icon" aria-hidden="true">🕓</span>
              <div className="engine-warn-body">
                <h3>Change held for your approval</h3>
                <p>
                  {result && result.short && <code>{result.short}</code>}{" "}
                  {result && result.message}
                  <br />
                  Review the agent's steps above, then approve to reboot into it — or reject to
                  discard and stay put. (Source diffs are operator-only.)
                </p>
                <div className="engine-warn-actions">
                  <Button variant="primary" onClick={approvePending} disabled={approving || !pendingSha}>
                    {approving ? "Working…" : "Approve & reboot"}
                  </Button>
                  <Button variant="danger" onClick={rejectPending} disabled={approving || !pendingSha}>
                    Reject
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* Result summary (non-pending) */}
          {!running && result && outcome !== "pending" && (
            <div className="run-result">
              <div className="result-summary">
                <ResultBadge result={result} />
                {result.short && <code>{result.short}</code>}
                {result.message && <span className="muted">{result.message}</span>}
                {!result.promoted && result.reason && <span className="err">{result.reason}</span>}
                <Button onClick={() => setRawResult((r) => !r)}>{rawResult ? "Summary" : "Raw"}</Button>
              </div>
              {rawResult && <Pre className={result.promoted ? "ok" : "err"}>{JSON.stringify(result, null, 2)}</Pre>}
              {/* Nudge: agent finished but didn't commit — let the user nudge it to finish. */}
              {!result.promoted && !result.cancelled && outcome === "failed" && (
                <div className="nudge-box">
                  <p>
                    <strong>💡 The agent finished without proposing a commit.</strong>{" "}
                    The work so far is preserved in the log above. Nudge it to commit, or submit a
                    new request.
                  </p>
                  <div className="row">
                    <TextInput
                      value={steer}
                      onChange={(e) => setSteer(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key !== "Enter") return;
                        const m = steer.trim();
                        if (!m) return;
                        setSteer("");
                        beginChangeRequest(m);
                      }}
                      placeholder='e.g. "propose_commit now" or "fix the validation errors and commit"'
                      className="input nudge-input"
                    />
                    <Button
                      variant="primary"
                      disabled={!steer.trim()}
                      onClick={() => {
                        const m = steer.trim();
                        if (!m) return;
                        setSteer("");
                        beginChangeRequest(m);
                      }}
                    >
                      Nudge agent
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {!showRun && (
        <section className="selfmod-ready-stage harness-panel">
          <div className="selfmod-ready-copy">
            <span className="run-ready-kicker"><HarnessStatusDot state="idle" /> Workbench ready</span>
            <h2>Start with an outcome, not an implementation</h2>
            <p>Choose a mission pattern or write your own brief in the control rail. The agent will expose every tool call and gate as it works.</p>
          </div>
          <p className="muted" style={{ marginTop: 0 }}>
            Not sure where to start? Pick a template — it fills the request above, then hit
            Submit. Quine rewrites its own code to make it happen.
          </p>
          <div className="template-grid">
            {SELFMOD_TEMPLATES.map((t) => (
              <button
                key={t.title}
                type="button"
                className="template-card"
                onClick={() => useTemplate(t)}
              >
                <span className="template-title">{t.title}</span>
                <span className="template-desc">{t.desc}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {/* The frozen regression suite (Verification Gate). Hidden entirely until the gate is
          on or has history, so the tab stays clean for users who never enable it. */}
      {(verifierEnabled || checks.length > 0) && (
        <HarnessDisclosure
          className="selfmod-secondary"
          title="Verification checks"
          description="Frozen behavioral regressions every candidate must pass"
          badge={<Badge tone={checks.some((c) => c.last_result && !c.last_result.ok) ? "bad" : "good"}>
            {checks.filter((c) => c.status === "active").length} active
          </Badge>}
          attention={checks.some((c) => c.last_result && !c.last_result.ok)}
        >
          <div className="harness-disclosure-toolbar"><Button onClick={loadChecks}>Refresh checks</Button></div>
          <p className="muted" style={{ marginTop: 0 }}>
            Acceptance checks that passed at promotion are <strong>frozen</strong> here as the
            regression suite every future change must pass before going live — the safety net
            compounds with use. Checks retire automatically when their version is reverted or
            rolled back; disable one yourself if it has gone stale.
          </p>
          {checks.length === 0 ? (
            <Empty>No frozen checks yet — they appear when a verified change is promoted.</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Check</th>
                  <th>From</th>
                  <th>Status</th>
                  <th>Last run</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {checks.map((c) => (
                  <tr key={c.id} className={c.status === "active" ? "" : "off-main"}>
                    <td>
                      <div className="ver-msg">
                        {c.name}
                        {c.prompt && <span className="ver-origin muted">“{c.prompt}”</span>}
                      </div>
                    </td>
                    <td className="nowrap">
                      {c.origin_seq ? `v${c.origin_seq}` : (c.origin || "").slice(0, 8)}
                      {c.origin_label ? ` · ${c.origin_label}` : ""}
                    </td>
                    <td>
                      <Badge tone={c.status === "active" ? "good" : "muted"}>
                        {c.status === "active" ? "active"
                          : c.disabled_by === "lifecycle" ? "retired (off the line)"
                            : "disabled"}
                      </Badge>
                    </td>
                    <td>
                      {c.last_result ? (
                        <Badge tone={c.last_result.ok ? "good" : "bad"} title={c.last_result.detail}>
                          {c.last_result.ok ? "passed" : "failed"}
                        </Badge>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <Button onClick={() => toggleCheck(c)}>
                        {c.status === "active" ? "Disable" : "Enable"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </HarnessDisclosure>
      )}

      {/* Agent evals: the held-out benchmark gating changes to the agent's own runtime.
          Hidden until enabled or populated, like the checks card. */}
      {evalsInfo && (evalsInfo.enabled || (evalsInfo.tasks || []).length > 0) && (
        <HarnessDisclosure
          className="selfmod-secondary"
          title="Agent evals"
          description="Held-out missions that test the candidate agent with its own new runtime"
          badge={<Badge tone={evalsInfo.enabled ? "accent" : "muted"}>
            {(evalsInfo.tasks || []).filter((task) => task.enabled !== false).length} enabled
          </Badge>}
          attention={(evalsInfo.tasks || []).some((task) => task.last_result && !task.last_result.ok)}
        >
          <div className="harness-disclosure-toolbar">
            <Button onClick={benchmarkNow}
              disabled={benchRunning || (evalsInfo.tasks || []).length === 0}>
              {benchRunning ? "Benchmarking…" : "Benchmark current version"}
            </Button>
            <Button onClick={loadEvals}>Refresh</Button>
          </div>
          <p className="muted" style={{ marginTop: 0 }}>
            Held-out benchmark tasks for the <strong>agent itself</strong>. When a change
            touches the agent's own runtime{(evalsInfo.paths || []).length ? ` (${evalsInfo.paths.join(", ")})` : " (any change)"},
            the candidate must run every task below — using <em>its own</em> new brain — and
            pass validation before the change can ship. Tasks are stored kernel-side, out of
            the agent's reach.
          </p>
          <label className="check">
            <input type="checkbox" checked={!!evalsInfo.enabled} onChange={toggleEvalsGate} />{" "}
            Gate runtime changes on the benchmark (evals.enabled)
          </label>
          {(evalsInfo.tasks || []).length === 0 ? (
            <Empty>No benchmark tasks yet — add one below (e.g. a small representative change request).</Empty>
          ) : (
            <table className="table">
              <thead>
                <tr><th>Task</th><th>Status</th><th>Last run</th><th /></tr>
              </thead>
              <tbody>
                {evalsInfo.tasks.map((t) => (
                  <tr key={t.id} className={t.enabled === false ? "off-main" : ""}>
                    <td>
                      <div className="ver-msg">
                        {t.name}
                        <span className="ver-origin muted">“{t.prompt}”</span>
                      </div>
                    </td>
                    <td>
                      <Badge tone={t.enabled === false ? "muted" : "good"}>
                        {t.enabled === false ? "disabled" : "active"}
                      </Badge>
                    </td>
                    <td>
                      {t.last_result ? (
                        <Badge tone={t.last_result.ok ? "good" : "bad"}
                          title={`${t.last_result.detail || ""} (candidate ${t.last_result.version || "?"})`}>
                          {t.last_result.ok ? "passed" : "failed"}
                        </Badge>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <div className="row">
                        <Button onClick={() => toggleEvalTask(t)}>
                          {t.enabled === false ? "Enable" : "Disable"}
                        </Button>
                        <Button variant="danger" onClick={() => deleteEvalTask(t)}>Delete</Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="row mt">
            <TextInput value={evalName} maxLength={80} placeholder="task name (e.g. can add a route)"
              onChange={(e) => setEvalName(e.target.value)} />
            <TextInput value={evalPrompt} placeholder="the benchmark change request the agent must complete"
              onChange={(e) => setEvalPrompt(e.target.value)} style={{ flex: 1 }} />
            <Button variant="primary" onClick={addEvalTask}
              disabled={!evalName.trim() || !evalPrompt.trim()}>
              Add task
            </Button>
          </div>
        </HarnessDisclosure>
      )}

      {!running && convos.length > 0 && (
        <HarnessDisclosure
          className="selfmod-secondary"
          title="Previous conversations"
          description="Resume context, reuse a brief, or branch from a produced version"
          badge={<Badge tone="muted">{convos.length}</Badge>}
        >
          <p className="muted" style={{ marginTop: 0 }}>
            Past self-modify conversations. <strong>Continue from vN</strong> re-bases a new change on
            the version a conversation produced and resumes its context — for fixing a bug or improving
            that change. <strong>Reuse a prompt</strong> starts a fresh take; <strong>Resume</strong>
            reopens an uncommitted/cancelled one.
          </p>
          <div className="convo-list">
            {convos.map((c) => {
              const ver = c.task ? versionByTask[c.task] : null;
              return (
                <div key={c.id} className="convo-row">
                  <div className="convo-main">
                    <span className="convo-prompt">{c.prompt || "(no prompt)"}</span>
                    <span className="convo-meta muted">
                      {ver && <Badge tone="accent">v{ver.seq}</Badge>}{" "}
                      {new Date(c.ts * 1000).toLocaleString()} · {c.messages} msg{c.messages === 1 ? "" : "s"}
                    </span>
                  </div>
                  <div className="row convo-actions">
                    <Button onClick={() => viewConvo(c.id)}>View</Button>
                    <Button onClick={() => reusePrompt(c.prompt)}>Reuse prompt</Button>
                    {ver ? (
                      <Button variant="primary" onClick={() => continueFromConvo(c, ver)}>
                        Continue from v{ver.seq}
                      </Button>
                    ) : (
                      <Button variant="primary" onClick={() => resumeConvo(c)} disabled={busyConvo}>
                        Resume
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </HarnessDisclosure>
      )}

      {(status?.queue?.length || 0) > 0 && (
        <HarnessDisclosure
          className="selfmod-secondary"
          title="Queued requests"
          description="Durable missions waiting for the single-writer pipeline"
          badge={<Badge tone="warn">{status.queue_depth ?? status.queue.length} queued</Badge>}
          attention
        >
          <p className="muted" style={{ marginTop: 0 }}>
            Tasks run one at a time; these are waiting their turn (the backlog survives a
            restart). Dequeue removes one before it starts.
          </p>
          <div className="convo-list">
            {status.queue.map((q) => {
              const isRunning = q.task_id === status.current_task;
              return (
                <div key={q.task_id} className="convo-row">
                  <div className="convo-main">
                    <span className="convo-prompt">
                      {q.kind !== "change" && <Badge tone="accent">{q.kind}</Badge>} {q.prompt || "(no prompt)"}
                    </span>
                    <span className="convo-meta muted">
                      {q.task_id} · {isRunning ? "running now" : "queued"}
                      {q.enqueued_at ? ` · ${q.enqueued_at.replace("T", " ").slice(0, 19)}` : ""}
                    </span>
                  </div>
                  <div className="row convo-actions">
                    {isRunning ? (
                      <Spinner />
                    ) : (
                      <Button variant="danger" onClick={() => dequeue(q.task_id)}>
                        Dequeue
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </HarnessDisclosure>
      )}

      {viewing && (
        <Modal
          title="Conversation transcript"
          wide
          onClose={() => setViewing(null)}
          actions={
            <Button
              onClick={() => {
                const u = viewing.messages.find((m) => m.role === "user");
                if (u) reusePrompt(u.content);
                setViewing(null);
              }}
            >
              Reuse prompt
            </Button>
          }
        >
          <div className="convo-transcript">
            {viewing.messages.length === 0 && <Empty>No messages in this conversation.</Empty>}
            {viewing.messages.map((m, i) => (
              <div key={i} className={"convo-msg " + m.role}>
                <Badge tone={m.role === "user" ? "accent" : m.role === "tool" ? "muted" : ""}>
                  {m.role}
                </Badge>
                <div className="convo-content">
                  {m.content}
                  {m.has_tool_calls && <em className="muted"> [requested a tool call]</em>}
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}
