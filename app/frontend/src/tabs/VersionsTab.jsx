import React, { useEffect, useMemo, useState } from "react";
import { sysGet, sysPost, beginReconnect, apiUrl } from "../api.js";
import { Card, Button, Badge, Empty, Modal, TextInput } from "../components";
import { StatusBadge, Origin, VerificationBadge } from "../components/versionMeta.jsx";
import VersionGraph from "../components/VersionGraph.jsx";
import { useToast } from "../components/Toast.jsx";
import { useConfirm } from "../components/Confirm.jsx";

// Browse the full version lineage. Two views over the same data: an animated git-graph
// (default) that draws the real DAG — trunk lane, branch lanes, dashed revert/re-apply
// cross-edges — and the classic scannable list. Every version carries its registry
// identity (v<seq>, label, status, origin/task provenance, revert edges) and its place on
// the active line. Versions can be REVERTED (undo one change, keep everything after) or
// RE-APPLIED (recover an abandoned change) — both build a new version through the same
// validate → health-gated reboot pipeline as agent changes. Version *source* is never
// shown here — diff review is operator-only (the app's own code stays concealed).

const PAGE = 50;
const GRAPH_LIMIT = 500; // per-request cap (kernel clamps to 500)
const GRAPH_CAP = 1500; // safety bound on total nodes laid out

export default function VersionsTab({ onStatus, status, onNavigate }) {
  const [versions, setVersions] = useState([]);
  const [total, setTotal] = useState(0);
  const [pending, setPending] = useState([]); // versions committed but awaiting approval
  const [lines, setLines] = useState([]); // named lines (experiment branches)
  const [previews, setPreviews] = useState([]); // running preview environments
  const [busy, setBusy] = useState(false);
  const [labeling, setLabeling] = useState(null); // {sha, short, label} label modal
  const [labelText, setLabelText] = useState("");
  const [view, setView] = useState(() => {
    try {
      return localStorage.getItem("quine.versionsView") || "graph";
    } catch {
      return "graph";
    }
  });
  const [selected, setSelected] = useState(null); // graph selection → detail panel
  const toast = useToast();
  const confirm = useConfirm();

  const activeVersion = status?.active?.version || status?.slots?.active_version || "";
  const undoDepth = (status?.slots?.promotion_history || []).length;
  // Stable identity so VersionGraph's node/edge memo doesn't recompute every render.
  const pendingSet = useMemo(() => new Set(pending.map((p) => p.sha)), [pending]);
  const seqBySha = Object.fromEntries(versions.map((v) => [v.sha, v.seq]));
  const seqOf = (sha) => (seqBySha[sha] ? `v${seqBySha[sha]}` : (sha || "").slice(0, 8));
  // The graph reloads versions in place after an action; keep the selected card fresh.
  const selectedLive = versions.find((v) => v.sha === selected?.sha) || selected;

  async function loadPending() {
    const p = await sysGet("/pending");
    setPending(p.pending || []);
    try {
      const [l, pv] = await Promise.all([sysGet("/lines"), sysGet("/previews")]);
      setLines(l.lines || []);
      setPreviews(pv.previews || []);
    } catch {
      /* kernel unreachable */
    }
  }

  // ── preview environments + lines ─────────────────────────────────────────────────
  async function previewVersion(v) {
    setBusy(true);
    const { data } = await sysPost("/preview", { version: v.sha });
    setBusy(false);
    if (data.ok) {
      toast.ok(`Preview '${data.name}' running — opening…`);
      window.open(apiUrl(data.url), "_blank");
      await loadPending();
    } else {
      toast.err("Preview failed: " + (data.reason || "?"));
    }
  }
  async function respawnLinePreview(name) {
    setBusy(true);
    const { data } = await sysPost("/preview", { line: name });
    setBusy(false);
    if (data.ok) {
      toast.ok(`Preview for line '${name}' running — opening…`);
      window.open(apiUrl(data.url), "_blank");
      await loadPending();
    } else {
      toast.err("Preview failed: " + (data.reason || "?"));
    }
  }
  async function stopPreview(name) {
    setBusy(true);
    await sysPost("/preview/stop", { name });
    setBusy(false);
    await loadPending();
  }
  async function promotePreview(p) {
    await runReboot({
      title: "Promote preview",
      body: `Make what preview '${p.name}' is showing (${p.short}) the PRODUCTION version? ` +
        "The app reboots into it through the normal health + verification gates.",
      confirmLabel: "Promote to production",
      path: "/preview/promote",
      payload: { name: p.name },
      okMsg: `Promoting ${p.short}`,
    });
  }
  async function promoteLine(l) {
    await runReboot({
      title: `Promote line '${l.name}'`,
      body: `Ship line '${l.name}' (tip ${l.short}${l.ahead ? `, ${l.ahead} change${l.ahead === 1 ? "" : "s"} ahead` : ""}) ` +
        "to PRODUCTION? The app reboots into its tip through the normal health + verification " +
        "gates; the line's commits join the active line.",
      confirmLabel: "Promote line",
      path: "/line/promote",
      payload: { name: l.name },
      okMsg: `Promoting line '${l.name}'`,
    });
  }
  async function deleteLine(l) {
    const ok = await confirm({
      title: `Discard line '${l.name}'`,
      body: `Delete line '${l.name}' and stop its preview? Its versions stay in history ` +
        "(off the active line) and can be re-applied later.",
      danger: true,
      confirmLabel: "Discard line",
    });
    if (!ok) return;
    setBusy(true);
    const { data } = await sysPost("/line/delete", { name: l.name });
    setBusy(false);
    if (data.ok) toast.info(`Line '${l.name}' discarded.`);
    else toast.err("Discard failed: " + (data.reason || "?"));
    await reload();
  }
  // List view: paginated ("Load more" appends).
  async function load(offset = 0) {
    const data = await sysGet(`/versions?limit=${PAGE}&offset=${offset}`);
    setVersions((prev) => (offset === 0 ? data.versions || [] : [...prev, ...(data.versions || [])]));
    setTotal(data.total || 0);
    await loadPending();
  }
  // Graph view needs the whole DAG (so branch/cross edges resolve), not one page.
  async function loadGraph() {
    let all = [];
    let offset = 0;
    let tot = 0;
    for (;;) {
      const data = await sysGet(`/versions?limit=${GRAPH_LIMIT}&offset=${offset}`);
      const batch = data.versions || [];
      all = all.concat(batch);
      tot = data.total || all.length;
      offset += batch.length;
      if (batch.length === 0 || all.length >= tot || all.length >= GRAPH_CAP) break;
    }
    setVersions(all);
    setTotal(tot);
    await loadPending();
  }
  const reload = () => (view === "graph" ? loadGraph() : load());

  useEffect(() => {
    view === "graph" ? loadGraph() : load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Switching into the graph needs the full set if the list only paged part of it.
  useEffect(() => {
    if (view === "graph" && versions.length < total) loadGraph();
    try {
      localStorage.setItem("quine.versionsView", view);
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);
  // Default the graph's detail panel to the running version.
  useEffect(() => {
    if (view === "graph" && !selected && activeVersion) {
      const a = versions.find((v) => v.sha === activeVersion);
      if (a) setSelected(a);
    }
  }, [view, activeVersion, versions, selected]);

  // Shared handler for every action that promotes a new/old version (reboots the app).
  async function runReboot({ title, body, confirmLabel, danger = false, path, payload, okMsg }) {
    const ok = await confirm({ title, body, confirmLabel, danger });
    if (!ok) return;
    setBusy(true);
    const { data } = await sysPost(path, payload);
    setBusy(false);
    await reload();
    if (onStatus) onStatus();
    if (data.ok) {
      toast.ok(okMsg + " — reloading…");
      beginReconnect("Rebooting…");
    } else {
      toast.err((title + " failed: ") + (data.reason || "?"));
    }
  }

  async function approve(sha) {
    await runReboot({
      title: "Approve & promote",
      body: `Promote ${sha.slice(0, 8)} now? The app will reboot into it via the safe blue-green path.`,
      confirmLabel: "Approve & promote",
      path: "/approve",
      payload: { sha },
      okMsg: `Promoting ${sha.slice(0, 8)}`,
    });
  }
  async function reject(sha) {
    const ok = await confirm({
      title: "Reject version",
      body: `Reject ${sha.slice(0, 8)}? It stays in history but is never promoted.`,
      danger: true,
      confirmLabel: "Reject",
    });
    if (!ok) return;
    setBusy(true);
    await sysPost("/reject", { sha });
    setBusy(false);
    await reload();
    toast.info(`Rejected ${sha.slice(0, 8)}.`);
  }

  async function rollbackTo(v) {
    await runReboot({
      title: "Roll back",
      body: `Roll back to v${v.seq} (${v.short})? The whole line rewinds to it — versions after it ` +
        "leave the active line (they stay listed and can be re-applied later). The app will reboot.",
      danger: true,
      confirmLabel: "Roll back",
      path: "/rollback_to",
      payload: { version: v.sha },
      okMsg: `Rolling back to v${v.seq}`,
    });
  }
  async function rollbackOne() {
    await runReboot({
      title: "Roll back one step",
      body: "Roll back to the previous promoted version? The app will reboot. " +
        `(${undoDepth} undo step${undoDepth === 1 ? "" : "s"} available.)`,
      danger: true,
      confirmLabel: "Roll back",
      path: "/rollback",
      payload: {},
      okMsg: "Rolling back",
    });
  }
  async function revert(v) {
    await runReboot({
      title: "Revert version",
      body: `Undo v${v.seq} (${v.short}) "${v.message}"? This creates a NEW version (the next ` +
        `v-number) with just this change removed — everything after it is KEPT, and v${v.seq} stays ` +
        "in history. It is validated and rebooted through the health gate; if it doesn't apply " +
        "cleanly nothing changes.",
      danger: true,
      confirmLabel: "Revert",
      path: "/revert",
      payload: { version: v.sha },
      okMsg: `Reverted v${v.seq}`,
    });
  }
  async function reapply(v) {
    await runReboot({
      title: "Re-apply version",
      body: `Re-apply v${v.seq} (${v.short}) "${v.message}"? This does NOT reactivate v${v.seq} in ` +
        `place — it creates a NEW version (the next v-number) containing v${v.seq}'s change applied ` +
        `onto the current line. v${v.seq} stays in history, off the line. It's validated and rebooted ` +
        "through the health gate; if it doesn't apply cleanly nothing changes.",
      confirmLabel: "Re-apply",
      path: "/reapply",
      payload: { version: v.sha },
      okMsg: `Re-applied v${v.seq}`,
    });
  }

  // "Continue from a commit": hand off to Self-Modify to start a NEW change based on this
  // version's tree (and, if it has one, its original conversation). Continuing anything but the
  // running version branches the line — warn about that. The actual run is driven from the
  // composer (base_version + resume_task), so here we just stash the intent and navigate.
  async function continueVersion(v) {
    const isActive = v.sha === activeVersion;
    const withConvo = v.task ? " and resume its original conversation" : "";
    const ok = await confirm({
      title: `Continue from v${v.seq}`,
      body: isActive
        ? `Start a new change based on the running version v${v.seq} (${v.short})${withConvo}. ` +
          "Good for fixing a bug or improving the change you just made."
        : `Start a new change based on v${v.seq} (${v.short}) "${v.message}" — the agent edits from ` +
          `THIS version's code${withConvo}. Because it isn't the running version, the new change ` +
          `BRANCHES from v${v.seq}: once it goes live, versions after it leave the active line ` +
          "(they stay listed and can be re-applied).",
      confirmLabel: "Continue in Self-Modify",
      danger: !isActive,
    });
    if (!ok) return;
    try {
      localStorage.setItem("quine-continue-intent", JSON.stringify({
        sha: v.sha, short: v.short, seq: v.seq, label: v.label || null,
        task: v.task || null, message: v.message || "", branch: !isActive,
      }));
    } catch {
      /* ignore */
    }
    if (onNavigate) onNavigate("modify");
  }

  function openLabel(v) {
    setLabelText(v.label || "");
    setLabeling(v);
  }
  async function saveLabel() {
    const v = labeling;
    setLabeling(null);
    const { data } = await sysPost("/label", { version: v.sha, label: labelText.trim() });
    if (data.ok) {
      toast.ok(labelText.trim() ? `Labeled v${v.seq} "${labelText.trim()}".` : `Cleared label on v${v.seq}.`);
    } else {
      toast.err("Label failed: " + (data.reason || "?"));
    }
    await reload();
  }

  const viewToggle = (
    <div className="seg" role="tablist" aria-label="Version view">
      <button
        className={"seg-btn" + (view === "graph" ? " active" : "")}
        onClick={() => setView("graph")}
        role="tab"
        aria-selected={view === "graph"}
      >
        ⌥ Graph
      </button>
      <button
        className={"seg-btn" + (view === "list" ? " active" : "")}
        onClick={() => setView("list")}
        role="tab"
        aria-selected={view === "list"}
      >
        ☰ List
      </button>
    </div>
  );

  return (
    <div className="stack">
      {pending.length > 0 && (
        <Card title="Pending approval">
          <p className="muted" style={{ marginTop: 0 }}>
            These versions are committed but not live. Approve (promotes via the safe blue-green
            reboot) or reject (discards; kept in history). Source diff review is operator-only.
          </p>
          <table className="table versions">
            <thead>
              <tr>
                <th>Version</th>
                <th>Message</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {pending.map((p) => (
                <tr key={p.sha}>
                  <td>
                    <div className="ver-cell">
                      <code>{p.short}</code>
                      <Badge tone="warn">pending</Badge>
                    </div>
                  </td>
                  <td>{p.message}</td>
                  <td>
                    <div className="row">
                      <Button onClick={() => previewVersion(p)} disabled={busy}
                        title="Boot this candidate as a preview environment and click around before deciding">
                        ◫ Preview
                      </Button>
                      <Button variant="primary" onClick={() => approve(p.sha)} disabled={busy}>
                        ✓ Approve
                      </Button>
                      <Button variant="danger" onClick={() => reject(p.sha)} disabled={busy}>
                        Reject
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {(lines.length > 0 || previews.length > 0) && (
        <Card title="Previews & lines">
          <p className="muted" style={{ marginTop: 0 }}>
            <strong>Lines</strong> are parallel version lines (experiments, staging, A/B
            variants) with their own preview URL — promote one to ship it, discard to drop
            it. <strong>Previews</strong> are running copies of a version this browser can
            open; they stop automatically when idle. Start line changes from the{" "}
            <a onClick={() => onNavigate && onNavigate("modify")} style={{ cursor: "pointer" }}>
              Self-Modify tab
            </a>.
          </p>
          {lines.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>Line</th>
                  <th>Tip</th>
                  <th>vs prod</th>
                  <th>Preview</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.name}>
                    <td>
                      <strong>⑂ {l.name}</strong>
                      {l.description && <div className="muted" style={{ fontSize: "0.85em" }}>{l.description}</div>}
                    </td>
                    <td className="nowrap">
                      {l.seq ? `v${l.seq}` : ""} <code>{l.short}</code>
                    </td>
                    <td className="muted nowrap">
                      {l.ahead ? `+${l.ahead}` : ""}{l.ahead && l.behind ? " / " : ""}
                      {l.behind ? `−${l.behind}` : ""}{!l.ahead && !l.behind ? "even" : ""}
                    </td>
                    <td>
                      {l.preview ? (
                        <a href={apiUrl(l.preview.url)} target="_blank" rel="noreferrer">
                          open ↗
                        </a>
                      ) : (
                        <Button onClick={() => respawnLinePreview(l.name)} disabled={busy}>
                          ▶ Start
                        </Button>
                      )}
                    </td>
                    <td>
                      <div className="row">
                        <Button variant="primary" onClick={() => promoteLine(l)} disabled={busy}
                          title="Ship this line to production (health + verification gated)">
                          ↑ Promote
                        </Button>
                        <Button variant="danger" onClick={() => deleteLine(l)} disabled={busy}>
                          Discard
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {previews.filter((p) => !p.line).length > 0 && (
            <table className="table" style={{ marginTop: lines.length ? 12 : 0 }}>
              <thead>
                <tr>
                  <th>Preview</th>
                  <th>Version</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {previews.filter((p) => !p.line).map((p) => (
                  <tr key={p.name}>
                    <td>
                      <strong>◫ {p.name}</strong>{" "}
                      <a href={apiUrl(p.url)} target="_blank" rel="noreferrer">open ↗</a>
                    </td>
                    <td className="nowrap"><code>{p.short}</code></td>
                    <td>
                      <div className="row">
                        <Button variant="primary" onClick={() => promotePreview(p)} disabled={busy}
                          title="Make this the production version">
                          ↑ Promote
                        </Button>
                        <Button onClick={() => stopPreview(p.name)} disabled={busy}>
                          ■ Stop
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}

      <Card
        title="Version history"
        actions={
          <>
            {viewToggle}
            <Button onClick={reload}>Refresh</Button>
            <Button onClick={rollbackOne} disabled={busy || (undoDepth === 0 && !status?.slots?.previous_version)}>
              ↶ Roll back one step{undoDepth > 0 ? ` (${undoDepth})` : ""}
            </Button>
          </>
        }
      >
        <details className="banner info" style={{ marginBottom: 12 }}>
          <summary>How versions work — reverting & re-applying always make a new version</summary>
          <p>
            History is <strong>append-only</strong>: <strong>Roll back</strong>,{" "}
            <strong>Revert</strong> and <strong>Re-apply</strong> never edit or delete an existing
            version — each one records a <strong>new version</strong> through the same
            validate → health-gated reboot as a normal change. Nothing is ever lost, and every step
            is auditable and itself reversible.
          </p>
          <p>
            So re-applying <code>v2</code> doesn't turn v2 back on in place — it creates a new{" "}
            <code>v3</code> whose contents are v2's change re-applied onto the current line. The
            original <code>v2</code> stays in history (off the active line); the live code is now{" "}
            <code>v3</code>. Reverting works the same way: undoing <code>v2</code> writes a new
            version with just that change removed, keeping everything after it.
          </p>
          <p>
            Want v2's <em>exact</em> tree back as the running version instead? Use{" "}
            <strong>Roll back</strong> to v2 — that rewinds the active line to it (later versions
            leave the line but stay re-applyable).
          </p>
        </details>
        {view === "graph" ? (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              The <strong>trunk</strong> is the active line; branches that left it (rolled back /
              abandoned) sit in their own lanes, and dashed links show <strong>reverts</strong> and{" "}
              <strong>re-applies</strong>. Drag to pan, scroll to zoom, click a version to act on it.
            </p>
            {versions.length === 0 ? (
              <Empty>No versions yet.</Empty>
            ) : (
              <div className="vg-layout">
                <VersionGraph
                  versions={versions}
                  activeVersion={activeVersion}
                  pendingSet={pendingSet}
                  selectedSha={selected?.sha}
                  onSelect={setSelected}
                />
                <aside className="vg-detail">
                  {selectedLive ? (
                    (() => {
                      const v = selectedLive;
                      const isActive = v.sha === activeVersion;
                      const canRevert = v.on_main && v.parent && !isActive;
                      const canReapply = !v.on_main && v.status !== "pending" && !pendingSet.has(v.sha);
                      const isPending = pendingSet.has(v.sha);
                      return (
                        <div className="vg-detail-card">
                          <div className="vg-detail-head">
                            <strong>{v.seq ? "v" + v.seq : "—"}</strong>
                            <StatusBadge v={v} isActive={isActive} />
                            <VerificationBadge v={v} />
                            {isPending && v.status !== "pending" && <Badge tone="warn">pending</Badge>}
                          </div>
                          <div className="vg-detail-sub">
                            <code>{v.short}</code>
                            {v.label && <Badge tone="muted">🏷 {v.label}</Badge>}
                          </div>
                          <div className="vg-detail-msg">{v.message}</div>
                          <Origin v={v} seqOf={seqOf} />
                          {v.verification?.failed?.length > 0 && (
                            <p className="err" style={{ margin: "6px 0 0", fontSize: "0.85em" }}>
                              ◎ {v.verification.failed[0].name}: {v.verification.failed[0].detail}
                            </p>
                          )}
                          <dl className="vg-detail-meta">
                            <div>
                              <dt>Date</dt>
                              <dd>{(v.date || "").replace("T", " ").slice(0, 16)}</dd>
                            </div>
                            <div>
                              <dt>Line</dt>
                              <dd>
                                {v.on_main
                                  ? isActive
                                    ? "active (running)"
                                    : "on the active line"
                                  : "off the active line"}
                              </dd>
                            </div>
                            {v.parent && (
                              <div>
                                <dt>Parent</dt>
                                <dd>{seqOf(v.parent)}</dd>
                              </div>
                            )}
                          </dl>
                          <div className="row vg-detail-actions">
                            <Button variant="primary" onClick={() => continueVersion(v)} disabled={busy}
                              title="Start a new change based on this version (and resume its conversation) — fix a bug or improve it">
                              ✎ Continue
                            </Button>
                            <Button onClick={() => previewVersion(v)} disabled={busy}
                              title="Boot this version as a preview environment — click around without touching production">
                              ◫ Preview
                            </Button>
                            <Button onClick={() => openLabel(v)} title="Set a name for this version">
                              🏷 Label
                            </Button>
                            {isPending && (
                              <Button variant="primary" onClick={() => approve(v.sha)} disabled={busy}>
                                ✓ Approve
                              </Button>
                            )}
                            {isPending && (
                              <Button variant="danger" onClick={() => reject(v.sha)} disabled={busy}>
                                Reject
                              </Button>
                            )}
                            {canRevert && (
                              <Button variant="danger" onClick={() => revert(v)} disabled={busy}
                                title="Undo just this version's changes; keep everything after it">
                                Revert
                              </Button>
                            )}
                            {canReapply && (
                              <Button variant="primary" onClick={() => reapply(v)} disabled={busy}
                                title="Bring this abandoned version's changes back onto the current line">
                                Re-apply
                              </Button>
                            )}
                            <Button
                              variant="danger"
                              onClick={() => rollbackTo(v)}
                              disabled={busy || isActive || !v.on_main}
                              title={isActive ? "This is the active version"
                                : !v.on_main ? "Off the active line — use Re-apply instead"
                                : "Rewind the whole line to this version"}
                            >
                              Roll back
                            </Button>
                          </div>
                        </div>
                      );
                    })()
                  ) : (
                    <p className="muted">Click a version in the graph to see its details and actions.</p>
                  )}
                </aside>
              </div>
            )}
          </>
        ) : (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              The rail marks the <strong>active line</strong>; dimmed rows left it (rolled back /
              abandoned / rejected) but stay recoverable. <strong>Revert</strong> undoes one version and
              keeps the rest; <strong>Re-apply</strong> brings an abandoned one back.
            </p>
            <table className="table versions">
              <thead>
                <tr>
                  <th aria-label="lineage" />
                  <th>#</th>
                  <th>Version</th>
                  <th>Message</th>
                  <th>Date</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => {
                  const isActive = v.sha === activeVersion;
                  const canRevert = v.on_main && v.parent && !isActive;
                  const canReapply = !v.on_main && v.status !== "pending" && !pendingSet.has(v.sha);
                  return (
                    <tr key={v.sha} className={(isActive ? "is-active " : "") + (v.on_main ? "" : "off-main")}>
                      <td className={"timeline " + (v.on_main ? "on-line" : "off-line")}>
                        <span
                          className="tl-dot"
                          title={v.on_main ? "on the active line" : "left the active line"}
                        />
                      </td>
                      <td className="nowrap">
                        <strong>{v.seq ? "v" + v.seq : "—"}</strong>
                        {v.label && <Badge tone="muted">🏷 {v.label}</Badge>}
                      </td>
                      <td>
                        <div className="ver-cell">
                          <code>{v.short}</code>
                          <StatusBadge v={v} isActive={isActive} />
                          <VerificationBadge v={v} />
                          {pendingSet.has(v.sha) && v.status !== "pending" && <Badge tone="warn">pending</Badge>}
                        </div>
                      </td>
                      <td>
                        <div className="ver-msg">
                          {v.message}
                          <Origin v={v} seqOf={seqOf} />
                        </div>
                      </td>
                      <td className="muted nowrap">{(v.date || "").replace("T", " ").slice(0, 16)}</td>
                      <td>
                        <div className="row">
                          <Button variant="primary" onClick={() => continueVersion(v)} disabled={busy}
                            title="Start a new change based on this version (and resume its conversation)">
                            ✎ Continue
                          </Button>
                          <Button onClick={() => previewVersion(v)} disabled={busy}
                            title="Boot this version as a preview environment">
                            ◫
                          </Button>
                          <Button onClick={() => openLabel(v)} title="Set a name for this version">
                            🏷
                          </Button>
                          {canRevert && (
                            <Button variant="danger" onClick={() => revert(v)} disabled={busy}
                              title="Undo just this version's changes; keep everything after it">
                              Revert
                            </Button>
                          )}
                          {canReapply && (
                            <Button variant="primary" onClick={() => reapply(v)} disabled={busy}
                              title="Bring this abandoned version's changes back onto the current line">
                              Re-apply
                            </Button>
                          )}
                          <Button
                            variant="danger"
                            onClick={() => rollbackTo(v)}
                            disabled={busy || isActive || !v.on_main}
                            title={isActive ? "This is the active version"
                              : !v.on_main ? "Off the active line — use Re-apply instead"
                              : "Rewind the whole line to this version"}
                          >
                            Roll back
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {versions.length === 0 && <Empty>No versions yet.</Empty>}
            {versions.length < total && (
              <div className="row mt" style={{ justifyContent: "center" }}>
                <Button onClick={() => load(versions.length)}>
                  Load more ({versions.length} of {total})
                </Button>
              </div>
            )}
          </>
        )}
      </Card>

      {labeling && (
        <Modal title={`Label v${labeling.seq} (${labeling.short})`} onClose={() => setLabeling(null)}>
          <p className="muted" style={{ marginTop: 0 }}>
            A unique, human-friendly name (e.g. <code>before-big-refactor</code>). Labels work
            anywhere a version is referenced — rollback, revert, re-apply. Leave empty to clear.
          </p>
          <div className="row">
            <TextInput
              value={labelText}
              onChange={(e) => setLabelText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && saveLabel()}
              placeholder="before-big-refactor"
              maxLength={64}
            />
            <Button variant="primary" onClick={saveLabel}>Save</Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
