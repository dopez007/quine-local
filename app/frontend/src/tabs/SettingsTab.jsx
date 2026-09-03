import React, { useEffect, useState } from "react";
import { sysGet, sysPost, apiUrl } from "../api.js";
import { Card, Button, Field, Select, TextInput, Pre } from "../components";
import Appearance from "../components/Appearance.jsx";
import AutomationCard from "../components/AutomationCard.jsx";

const OPTIONAL_TOOLS = ["list_dir", "run_shell", "run_tests"];

// Build "provider/model" suggestions for the model datalist from the kernel's litellm catalog.
// Preferred providers come first (the kernel orders them), so the cap keeps the common ones.
function buildModelSuggestions(providers) {
  const out = [];
  const seen = new Set();
  for (const p of providers || []) {
    for (const m of p.models || []) {
      const id = m.includes("/") ? m : `${p.name}/${m}`;
      if (!seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
      if (out.length >= 800) return out;
    }
  }
  return out;
}

// Runtime parameters live in the kernel-owned config (state/config.yaml). The agent
// can't write kernel files, so it/the user change them through the bounded config
// syscall surfaced here. Applies to the next self-modification.
export default function SettingsTab() {
  const [cfg, setCfg] = useState(null);
  const [draft, setDraft] = useState(null);
  const [agents, setAgents] = useState([]); // editable preset list (config.agents)
  const [modelSuggest, setModelSuggest] = useState([]);
  const [agentsMsg, setAgentsMsg] = useState(null);
  const [savingAgents, setSavingAgents] = useState(false);
  const [backendCfg, setBackendCfg] = useState(null);
  const [backendDraft, setBackendDraft] = useState({ max_rounds: 200 });
  const [msg, setMsg] = useState(null);
  const [backendMsg, setBackendMsg] = useState(null);
  const [saving, setSaving] = useState(false);

  async function load() {
    const { config } = await sysGet("/config");
    setCfg(config);
    setAgents((config.agents || []).map((a) => ({ ...a })));
    setDraft({
      "agent.engine": config.agent.engine,
      "agent.model": config.agent.model,
      "agent.max_steps": config.agent.max_steps,
      "agent.tools_enabled": config.agent.tools_enabled || [],
      "watchdog.health_timeout_seconds": config.watchdog.health_timeout_seconds,
    });
    // litellm-derived model catalog → datalist suggestions (best-effort; agents stay editable
    // as free text if the catalog can't be fetched).
    try {
      const cat = await sysGet("/models");
      setModelSuggest(buildModelSuggestions(cat.providers));
    } catch { /* ignore */ }
    // Also load backend config
    try {
      const bc = await (await fetch(apiUrl("/api/agent/config"))).json();
      setBackendCfg(bc);
      setBackendDraft({ max_rounds: bc.max_rounds ?? 200 });
    } catch { /* ignore */ }
  }
  useEffect(() => {
    load();
  }, []);

  if (!draft) {
    return (
      <Card title="Settings">
        <span className="muted">loading…</span>
      </Card>
    );
  }

  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const toggleTool = (t) =>
    set(
      "agent.tools_enabled",
      draft["agent.tools_enabled"].includes(t)
        ? draft["agent.tools_enabled"].filter((x) => x !== t)
        : [...draft["agent.tools_enabled"], t],
    );

  // ── agent presets editor ──────────────────────────────────────────────────────────
  const setAgent = (i, k, v) =>
    setAgents((list) => list.map((a, j) => (j === i ? { ...a, [k]: v } : a)));
  const addAgent = () =>
    setAgents((list) => [...list, { name: "", model: "", engine: "litellm" }]);
  const removeAgent = (i) => setAgents((list) => list.filter((_, j) => j !== i));

  async function saveAgents() {
    setSavingAgents(true);
    setAgentsMsg(null);
    // Drop blank rows; the kernel validates name+model+engine and rejects the whole patch if any
    // row is malformed, so nothing is half-saved.
    const cleaned = agents
      .map((a) => ({
        name: (a.name || "").trim(),
        model: (a.model || "").trim(),
        engine: a.engine === "scripted" ? "scripted" : "litellm",
      }))
      .filter((a) => a.name && a.model);
    const { status, data } = await sysPost("/config", { patch: { agents: cleaned } });
    setSavingAgents(false);
    if (status === 200 && data.ok) {
      setAgentsMsg({ tone: "ok", text: "Agents saved." });
      setCfg(data.config);
      setAgents((data.config.agents || []).map((a) => ({ ...a })));
    } else {
      setAgentsMsg({ tone: "err", text: "Failed: " + ((data.errors || []).join("; ") || "?") });
    }
  }

  async function save() {
    setSaving(true);
    setMsg(null);
    const { status, data } = await sysPost("/config", {
      patch: {
        "agent.engine": draft["agent.engine"],
        "agent.model": draft["agent.model"],
        "agent.max_steps": parseInt(draft["agent.max_steps"], 10),
        "agent.tools_enabled": draft["agent.tools_enabled"],
        "watchdog.health_timeout_seconds": parseInt(draft["watchdog.health_timeout_seconds"], 10),
      },
    });
    setSaving(false);
    if (status === 200 && data.ok) {
      setMsg({ tone: "ok", text: "Saved." });
      setCfg(data.config);
    } else {
      setMsg({ tone: "err", text: "Failed: " + ((data.errors || []).join("; ") || "?") });
    }
  }

  async function saveBackend() {
    setBackendMsg(null);
    try {
      const r = await fetch(apiUrl("/api/agent/config"), {
        method: "PUT",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({ max_rounds: parseInt(backendDraft.max_rounds, 10) }),
      });
      const d = await r.json();
      if (d.ok) {
        setBackendMsg({ tone: "ok", text: "Saved. Next Run tab message will use it." });
        setBackendCfg(d.config);
      } else {
        setBackendMsg({ tone: "err", text: "Failed: " + (d.error || "?") });
      }
    } catch (e) {
      setBackendMsg({ tone: "err", text: "Error: " + String(e) });
    }
  }

  return (
    <div className="stack">
      {/* One shared datalist of model ids; every model input below references it. */}
      <datalist id="model-suggest">
        {modelSuggest.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>

      <Card title="Appearance">
        <p className="muted">Choose a theme. Saved on this device.</p>
        <Appearance inline />
      </Card>

      <Card title="Agent settings (self-modify agent)">
        <p className="muted">
          Runtime parameters the kernel reads. The agent can’t edit kernel code, so it
          changes them here — bounded and allow-listed. Applies to the next
          self-modification.
        </p>
        {agents.length > 0 && (
          <Field label="Agent preset (sets engine + model below)">
            <Select
              value={draft["agent.model"]}
              options={[
                ...(agents.some((a) => a.model === draft["agent.model"])
                  ? []
                  : [{ value: draft["agent.model"], label: "Custom: " + draft["agent.model"] }]),
                ...agents.map((a) => ({ value: a.model, label: `${a.name} — ${a.model}` })),
              ]}
              onChange={(e) => {
                const a = agents.find((x) => x.model === e.target.value);
                set("agent.model", e.target.value);
                if (a && a.engine) set("agent.engine", a.engine);
              }}
            />
          </Field>
        )}
        <div className="grid2">
          <Field label="Engine">
            <Select
              value={draft["agent.engine"]}
              options={["scripted", "litellm"]}
              onChange={(e) => set("agent.engine", e.target.value)}
            />
          </Field>
          <Field label="Model">
            <TextInput
              list="model-suggest"
              value={draft["agent.model"]}
              onChange={(e) => set("agent.model", e.target.value)}
            />
          </Field>
          <Field label="Max steps (1–200)">
            <TextInput
              type="number"
              min="1"
              max="200"
              value={draft["agent.max_steps"]}
              onChange={(e) => set("agent.max_steps", e.target.value)}
            />
          </Field>
        </div>

        <Field label="Enabled tools (read_file / write_file / propose_commit are always on)">
          <div className="row">
            {OPTIONAL_TOOLS.map((t) => (
              <label key={t} className="check">
                <input
                  type="checkbox"
                  checked={draft["agent.tools_enabled"].includes(t)}
                  onChange={() => toggleTool(t)}
                />{" "}
                {t}
              </label>
            ))}
          </div>
        </Field>

        <Field label="Watchdog health timeout, seconds (5–600)">
          <TextInput
            type="number"
            min="5"
            max="600"
            value={draft["watchdog.health_timeout_seconds"]}
            onChange={(e) => set("watchdog.health_timeout_seconds", e.target.value)}
          />
        </Field>

        <div className="row mt">
          <Button variant="primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </Button>
          {msg && <span className={msg.tone === "ok" ? "ok" : "err"}>{msg.text}</span>}
        </div>
      </Card>

      <Card
        title="Agents (model presets)"
        actions={<Button onClick={addAgent}>+ Add agent</Button>}
      >
        <p className="muted">
          The presets offered in the picker above (and the Run tab). Pick any litellm model —
          type a <code>provider/model</code> id or choose from the suggestions; set its key in
          the workspace’s control panel (Bring Your Own Key). Saved to the kernel config.
        </p>
        {agents.length === 0 ? (
          <p className="muted">No agents yet — add one to get started.</p>
        ) : (
          <div className="stack">
            {agents.map((a, i) => (
              <div className="agent-row" key={i}>
                <TextInput
                  placeholder="Name (e.g. Claude Opus)"
                  value={a.name}
                  onChange={(e) => setAgent(i, "name", e.target.value)}
                />
                <TextInput
                  list="model-suggest"
                  placeholder="provider/model (e.g. anthropic/claude-opus-4-8)"
                  value={a.model}
                  onChange={(e) => setAgent(i, "model", e.target.value)}
                />
                <Select
                  value={a.engine || "litellm"}
                  options={["litellm", "scripted"]}
                  onChange={(e) => setAgent(i, "engine", e.target.value)}
                />
                <Button variant="ghost danger" onClick={() => removeAgent(i)} title="Remove">
                  ✕
                </Button>
              </div>
            ))}
          </div>
        )}
        <div className="row mt">
          <Button variant="primary" onClick={saveAgents} disabled={savingAgents}>
            {savingAgents ? "Saving…" : "Save agents"}
          </Button>
          {agentsMsg && (
            <span className={agentsMsg.tone === "ok" ? "ok" : "err"}>{agentsMsg.text}</span>
          )}
        </div>
      </Card>

      <Card title="Run tab settings">
        <p className="muted">
          Settings for the Run tab's chat agent (in-app assistant). These take effect on
          the next message sent.
        </p>
        <Field label="Max tool rounds per message (1–500; 200 ≈ no limit)">
          <TextInput
            type="number"
            min="1"
            max="500"
            value={backendDraft.max_rounds}
            onChange={(e) => setBackendDraft((d) => ({ ...d, max_rounds: e.target.value }))}
          />
        </Field>
        <div className="row mt">
          <Button variant="primary" onClick={saveBackend}>
            Save Run tab settings
          </Button>
          {backendMsg && <span className={backendMsg.tone === "ok" ? "ok" : "err"}>{backendMsg.text}</span>}
        </div>
      </Card>

      <AutomationCard />

      <Card title="Current config (read-only)">
        <Pre className="log">{JSON.stringify(cfg, null, 2)}</Pre>
      </Card>
    </div>
  );
}
