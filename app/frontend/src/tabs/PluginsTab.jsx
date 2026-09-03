import React, { useEffect, useState } from "react";
import { appGet, appPost, appDelete } from "../api.js";
import { Card, Button, Badge, Empty, Field, TextInput, TextArea } from "../components";
import { useToast } from "../components/Toast.jsx";
import { useConfirm } from "../components/Confirm.jsx";

// The in-app plugin SDK. Browse installed plugins, toggle them, install one from source, or
// uninstall. Backend:
//   GET    /api/plugins
//   POST   /api/plugins/install            {name, source}
//   POST   /api/plugins/{name}/{enable|disable}
//   DELETE /api/plugins/{name}
export default function PluginsTab() {
  const [plugins, setPlugins] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(""); // plugin name (or "__install") currently mutating
  const [showInstall, setShowInstall] = useState(false);
  const [form, setForm] = useState({ name: "", source: "" });
  const [msg, setMsg] = useState(null); // in-form validation only: { tone: "err", text }
  const toast = useToast();
  const confirm = useConfirm();

  async function load() {
    setLoading(true);
    try {
      const { plugins } = await appGet("/api/plugins");
      setPlugins(plugins || []);
    } catch {
      toast.err("Could not load plugins.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function toggle(p) {
    setBusy(p.name);
    const d = await appPost(`/api/plugins/${p.name}/${p.enabled ? "disable" : "enable"}`);
    if (d.error) toast.err(d.error);
    else toast.ok(`${p.enabled ? "Disabled" : "Enabled"} ${p.name}.`);
    await load();
    setBusy("");
  }

  async function uninstall(p) {
    const ok = await confirm({
      title: "Uninstall plugin",
      body: `Uninstall "${p.name}"? This deletes its source file.`,
      danger: true,
      confirmLabel: "Uninstall",
    });
    if (!ok) return;
    setBusy(p.name);
    const d = await appDelete(`/api/plugins/${p.name}`);
    if (d.error) toast.err(d.error);
    else toast.ok(`Uninstalled ${p.name}.${d.note ? " " + d.note : ""}`);
    await load();
    setBusy("");
  }

  async function install(e) {
    e.preventDefault();
    setMsg(null);
    const name = form.name.trim();
    if (!name || !form.source.trim()) {
      setMsg({ tone: "err", text: "Name and source are both required." });
      return;
    }
    setBusy("__install");
    const d = await appPost("/api/plugins/install", { name, source: form.source });
    if (d.error) {
      toast.err(d.error);
    } else {
      toast.ok(`Installed ${d.plugin?.name || name}.`);
      setForm({ name: "", source: "" });
      setShowInstall(false);
    }
    await load();
    setBusy("");
  }

  const chips = (items, empty = "—") =>
    items && items.length ? (
      items.map((t) => (
        <code key={typeof t === "string" ? t : t.path} className="chip">
          {typeof t === "string" ? t : t.path}
        </code>
      ))
    ) : (
      <span className="muted">{empty}</span>
    );

  return (
    <Card
      title="Plugins"
      actions={
        <>
          <Button onClick={() => setShowInstall((s) => !s)}>
            {showInstall ? "Cancel" : "Install plugin"}
          </Button>
          <Button onClick={load}>Refresh</Button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0 }}>
        Plugins add HTTP routes and Run-agent tools. Disable one to take its tools and routes
        offline without removing it; built-in plugins can be toggled but not uninstalled. The
        agent can also write a new plugin itself via Self-Modify.
      </p>

      {showInstall && (
        <form className="install-form" onSubmit={install}>
          {msg && <div className={"banner " + msg.tone}>{msg.text}</div>}
          <Field label="Plugin name" hint="a–z, 0–9, _ (3–41 chars)">
            <TextInput
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="my_plugin"
              autoFocus
            />
          </Field>
          <Field label="Source (Python)" hint="Must define a PLUGIN dict; optional router / TOOLS.">
            <TextArea
              rows={10}
              value={form.source}
              onChange={(e) => setForm({ ...form, source: e.target.value })}
              placeholder={'PLUGIN = {"name": "my_plugin", "version": "1.0.0", "description": "..."}'}
              spellCheck={false}
            />
          </Field>
          <div className="row">
            <Button variant="primary" type="submit" disabled={busy === "__install"}>
              {busy === "__install" ? "Installing…" : "Install"}
            </Button>
          </div>
        </form>
      )}

      {loading ? (
        <Empty>Loading…</Empty>
      ) : plugins.length === 0 ? (
        <Empty>No plugins installed. Click “Install plugin” to add one.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Source</th>
              <th>Tools</th>
              <th>Routes</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {plugins.map((p) => (
              <tr key={p.name}>
                <td>
                  <strong>{p.name}</strong> <span className="muted mono">v{p.version}</span>
                  {p.description && <div className="muted">{p.description}</div>}
                  {p.error && (
                    <div>
                      <Badge tone="bad">error</Badge> <span className="muted">{p.error}</span>
                    </div>
                  )}
                </td>
                <td>
                  <Badge tone={p.source === "installed" ? "accent" : "muted"}>{p.source}</Badge>
                </td>
                <td>{chips(p.tools)}</td>
                <td>{chips(p.routes)}</td>
                <td>
                  <Badge tone={p.enabled ? "good" : "warn"}>
                    {p.enabled ? "enabled" : "disabled"}
                  </Badge>
                </td>
                <td className="nowrap">
                  <Button onClick={() => toggle(p)} disabled={busy === p.name || !!p.error}>
                    {p.enabled ? "Disable" : "Enable"}
                  </Button>
                  {p.source === "installed" && (
                    <Button variant="danger" onClick={() => uninstall(p)} disabled={busy === p.name}>
                      Uninstall
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
