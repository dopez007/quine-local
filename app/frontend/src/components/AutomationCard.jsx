import React, { useEffect, useState } from "react";
import { sysGet, sysPost } from "../api.js";
import { Card, Button, Badge, Field, TextInput, TextArea } from "./index.jsx";
import { useToast } from "./Toast.jsx";
import { useConfirm } from "./Confirm.jsx";

// Autonomous triggers ("Automation"): the kernel scheduler/event source that dispatches
// self-mods on a schedule, an HMAC'd webhook, or an error spike. This card is the operator
// surface — master switch, daily cap, full-auto opt-in, and per-trigger CRUD. Every
// autonomous change still flows through validate → verify → (by default) hold-for-approval,
// so the scary knob (auto_promote) is gated on the Verification Gate being on.

const KIND_LABEL = { schedule: "Schedule", webhook: "Webhook", error_spike: "Error spike", advisor: "Advisor auto-file" };

const BLANK = {
  name: "",
  kind: "error_spike",
  prompt_template: "",
  config: { threshold: 3, window_minutes: 60, interval_minutes: 60, daily_at: "03:00", max_per_tick: 1 },
  enabled: true,
};

function summarize(t) {
  const c = t.config || {};
  if (t.kind === "schedule") return c.interval_minutes ? `every ${c.interval_minutes} min` : `daily at ${c.daily_at}`;
  if (t.kind === "error_spike") return `≥ ${c.threshold ?? 3} in ${c.window_minutes ?? 60} min` + (c.trip_on_new ? " (or any new)" : "");
  if (t.kind === "advisor") return `auto-files ≤ ${c.max_per_tick ?? 1} open suggestion${(c.max_per_tick ?? 1) === 1 ? "" : "s"}/pass`;
  return "on signed POST";
}

export default function AutomationCard() {
  const [cfg, setCfg] = useState(null); // {enabled, max_per_day, auto_promote, verifier_enabled}
  const [triggers, setTriggers] = useState([]);
  const [editing, setEditing] = useState(null); // draft trigger or null
  const [secret, setSecret] = useState(null); // {url, secret, header} shown once after create
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  async function load() {
    try {
      const data = await sysGet("/triggers");
      setCfg(data.config);
      setTriggers(data.triggers || []);
    } catch {
      /* kernel unreachable */
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function patchConfig(patch) {
    const prev = cfg;
    setCfg((c) => ({ ...c, ...mapPatch(patch) })); // optimistic
    const { status, data } = await sysPost("/config", { patch });
    if (!(status === 200 && data && data.ok)) {
      setCfg(prev);
      toast.err("Config update failed.");
    }
  }
  function mapPatch(patch) {
    const m = {};
    for (const [k, v] of Object.entries(patch)) m[k.replace("triggers.", "")] = v;
    return m;
  }

  async function saveTrigger() {
    const t = editing;
    if (!t.name.trim() || !t.prompt_template.trim()) {
      toast.err("Name and prompt are required.");
      return;
    }
    // Send only the config keys this kind uses (keeps stored specs clean).
    const config =
      t.kind === "schedule"
        ? t.config.interval_minutes
          ? { interval_minutes: Number(t.config.interval_minutes) }
          : { daily_at: t.config.daily_at }
        : t.kind === "error_spike"
          ? {
              threshold: Number(t.config.threshold) || 3,
              window_minutes: Number(t.config.window_minutes) || 60,
              trip_on_new: !!t.config.trip_on_new,
            }
          : t.kind === "advisor"
            ? { max_per_tick: Math.round(Number(t.config.max_per_tick)) || 1 }
            : {};
    setBusy(true);
    const { status, data } = await sysPost("/triggers", {
      id: t.id, name: t.name.trim(), kind: t.kind, prompt_template: t.prompt_template, config,
    });
    setBusy(false);
    if (status === 200 && data && data.ok) {
      setEditing(null);
      if (data.secret_shown_once) {
        setSecret({ url: data.webhook_url, secret: data.secret_shown_once, header: data.signature_header });
      }
      toast.ok(t.id ? "Trigger updated." : "Trigger created.");
      await load();
    } else {
      toast.err("Save failed: " + ((data && data.reason) || "?"));
    }
  }

  async function toggleTrigger(t) {
    await sysPost("/triggers/toggle", { id: t.id, enabled: !t.enabled });
    await load();
  }
  async function deleteTrigger(t) {
    const ok = await confirm({ title: "Delete trigger", body: `Delete "${t.name}"?`, danger: true, confirmLabel: "Delete" });
    if (!ok) return;
    await sysPost("/triggers/delete", { id: t.id });
    await load();
  }

  if (!cfg) return null;
  const on = !!cfg.enabled;

  return (
    <Card title="Automation (autonomous triggers)">
      <p className="muted" style={{ marginTop: 0 }}>
        Let the app maintain and improve itself: dispatch a self-modification on a schedule, an
        incoming webhook, or an <strong>error spike</strong> (self-healing). Every autonomous change
        runs the full validate → verify pipeline and, by default, is <strong>held for your
        approval</strong> — you wake up to a verified, previewable fix awaiting one click.
      </p>

      <label className="check">
        <input type="checkbox" checked={on} onChange={() => patchConfig({ "triggers.enabled": !on })} />{" "}
        <strong>Enable automation</strong> (master switch)
      </label>

      {on && (
        <div className="automation-config">
          <Field label="Max autonomous changes per day">
            <TextInput
              type="number"
              min="1"
              max="100"
              value={cfg.max_per_day}
              onChange={(e) => patchConfig({ "triggers.max_per_day": Number(e.target.value) })}
            />
          </Field>
          <label className="check" title={cfg.verifier_enabled ? "" : "Requires the Verification Gate (Self-Modify tab) to be on"}>
            <input
              type="checkbox"
              checked={!!cfg.auto_promote}
              disabled={!cfg.verifier_enabled}
              onChange={() => patchConfig({ "triggers.auto_promote": !cfg.auto_promote })}
            />{" "}
            Full-auto: promote without approval when verified
          </label>
          {!cfg.verifier_enabled && (
            <span className="field-hint">
              Full-auto is locked until the <strong>Verification Gate</strong> is enabled (Self-Modify
              tab). A timer must never promote unverified code — autonomous changes hold for approval
              until then.
            </span>
          )}
        </div>
      )}

      <div className="row mt" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <strong>Triggers</strong>
        <Button onClick={() => setEditing({ ...BLANK })}>+ Add trigger</Button>
      </div>

      {triggers.length === 0 ? (
        <p className="muted">No triggers yet. Add one, or use the one-click Self-healing toggle in the Errors tab.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>When</th>
              <th>Last fired</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {triggers.map((t) => (
              <tr key={t.id} className={t.enabled ? "" : "off-main"}>
                <td><strong>{t.name}</strong></td>
                <td><Badge tone="muted">{KIND_LABEL[t.kind] || t.kind}</Badge></td>
                <td className="muted">{summarize(t)}</td>
                <td className="muted nowrap">
                  {t.last_fired ? new Date(t.last_fired * 1000).toLocaleString() : "—"}
                  {t.fires_today ? ` (${t.fires_today}× today)` : ""}
                </td>
                <td>
                  <div className="row">
                    <Button onClick={() => toggleTrigger(t)}>{t.enabled ? "Disable" : "Enable"}</Button>
                    <Button onClick={() => setEditing({ ...BLANK, ...t, config: { ...BLANK.config, ...(t.config || {}) } })}>Edit</Button>
                    <Button variant="danger" onClick={() => deleteTrigger(t)}>Delete</Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {editing && (
        <div className="trigger-editor">
          <h4>{editing.id ? "Edit trigger" : "New trigger"}</h4>
          <Field label="Name">
            <TextInput value={editing.name} maxLength={48} onChange={(e) => setEditing((t) => ({ ...t, name: e.target.value }))} placeholder="nightly-tidy" />
          </Field>
          <Field label="Kind">
            <select
              className="select"
              value={editing.kind}
              disabled={!!editing.id}
              onChange={(e) => setEditing((t) => ({ ...t, kind: e.target.value }))}
            >
              <option value="error_spike">Error spike (self-healing)</option>
              <option value="schedule">Schedule</option>
              <option value="webhook">Webhook</option>
              <option value="advisor">Advisor auto-file (self-improvement)</option>
            </select>
          </Field>

          {editing.kind === "schedule" && (
            <div className="row">
              <Field label="Every N minutes (blank ⇒ daily)">
                <TextInput type="number" min="1" value={editing.config.interval_minutes}
                  onChange={(e) => setEditing((t) => ({ ...t, config: { ...t.config, interval_minutes: e.target.value } }))} />
              </Field>
              <Field label="…or daily at (UTC HH:MM)">
                <TextInput value={editing.config.daily_at}
                  onChange={(e) => setEditing((t) => ({ ...t, config: { ...t.config, interval_minutes: "", daily_at: e.target.value } }))} />
              </Field>
            </div>
          )}
          {editing.kind === "error_spike" && (
            <div className="row">
              <Field label="Occurrence threshold">
                <TextInput type="number" min="1" value={editing.config.threshold}
                  onChange={(e) => setEditing((t) => ({ ...t, config: { ...t.config, threshold: e.target.value } }))} />
              </Field>
              <Field label="Within window (minutes)">
                <TextInput type="number" min="1" value={editing.config.window_minutes}
                  onChange={(e) => setEditing((t) => ({ ...t, config: { ...t.config, window_minutes: e.target.value } }))} />
              </Field>
              <label className="check" style={{ alignSelf: "end" }}>
                <input type="checkbox" checked={!!editing.config.trip_on_new}
                  onChange={(e) => setEditing((t) => ({ ...t, config: { ...t.config, trip_on_new: e.target.checked } }))} />{" "}
                also trip on any new error
              </label>
            </div>
          )}
          {editing.kind === "webhook" && (
            <p className="field-hint">
              A signed <code>POST /api/syscall/webhook/&lt;id&gt;</code> fires this trigger. The HMAC
              secret is shown once, after you save.
            </p>
          )}
          {editing.kind === "advisor" && (
            <>
              <div className="row">
                <Field label="Max proposals filed per pass (1–5)">
                  <TextInput type="number" min="1" max="5" value={editing.config.max_per_tick}
                    onChange={(e) => setEditing((t) => ({ ...t, config: { ...t.config, max_per_tick: e.target.value } }))} />
                </Field>
              </div>
              <p className="field-hint">
                Files the Advisor's open suggestions (Self-Modify tab) as change requests, one
                pass at a time — the full self-improvement loop with no clicks. Each proposal is
                filed at most once a month, and every run still obeys the daily cap and your
                approval rules below.
              </p>
            </>
          )}

          <Field label="Prompt template (the self-mod request)">
            <TextArea rows={3} value={editing.prompt_template}
              onChange={(e) => setEditing((t) => ({ ...t, prompt_template: e.target.value }))}
              placeholder={editing.kind === "error_spike"
                ? "Investigate and fix the error with fingerprint {{error.fingerprint}}: {{error.message}} on {{error.route}}. Traceback: {{error.traceback_tail}}"
                : editing.kind === "webhook"
                  ? "Handle this deploy event: {{payload}}"
                  : editing.kind === "advisor"
                    ? "{{proposal.prompt}}"
                    : "Review recent changes and tidy up any obvious issues."} />
          </Field>
          <p className="field-hint">
            Placeholders: <code>{"{{trigger.name}}"}</code>
            {editing.kind === "error_spike" && <>, <code>{"{{error.fingerprint}}"}</code>, <code>{"{{error.message}}"}</code>, <code>{"{{error.route}}"}</code>, <code>{"{{error.count}}"}</code>, <code>{"{{error.traceback_tail}}"}</code></>}
            {editing.kind === "webhook" && <>, <code>{"{{payload}}"}</code></>}
            {editing.kind === "advisor" && <>, <code>{"{{proposal.prompt}}"}</code>, <code>{"{{proposal.title}}"}</code>, <code>{"{{proposal.id}}"}</code></>}.
          </p>

          <div className="row mt">
            <Button variant="primary" onClick={saveTrigger} disabled={busy}>{editing.id ? "Save" : "Create"}</Button>
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
          </div>
        </div>
      )}

      {secret && (
        <div className="banner warn" style={{ marginTop: 12 }}>
          <strong>Webhook secret — copy it now (shown only once):</strong>
          <pre className="log" style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>{secret.secret}</pre>
          <div className="muted">POST to <code>{secret.url}</code> with <code>{secret.header}</code></div>
          <div className="row mt">
            <Button onClick={() => { navigator.clipboard?.writeText(secret.secret); toast.ok("Secret copied."); }}>Copy secret</Button>
            <Button variant="ghost" onClick={() => setSecret(null)}>Done</Button>
          </div>
        </div>
      )}
    </Card>
  );
}
