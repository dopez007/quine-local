import React, { useEffect, useState } from "react";
import { sysGet, sysPost, beginReconnect } from "../api.js";
import { Card, Button, Badge, TextArea, TextInput, Empty } from "../components";
import { useToast } from "../components/Toast.jsx";
import { useConfirm } from "../components/Confirm.jsx";

// Gated Kernel Self-Update (feature #4) — the operator surface for letting the agent rewrite
// the KERNEL itself. Deliberately stern: this is the highest-blast-radius capability in the
// system. Every kernel change is validated stricter than an app change, ALWAYS held for this
// approval, and applied by the immutable firmware which health-gates it and auto-rolls-back a
// kernel that won't boot. In signed mode the firmware also requires an operator ed25519
// signature over the candidate digest, so even a compromised kernel can't promote a new one.

function digestChip(d) {
  return d ? d.slice(0, 16) + "…" : "—";
}

export default function KernelTab() {
  const toast = useToast();
  const confirm = useConfirm();
  const [status, setStatus] = useState(null);
  const [versions, setVersions] = useState([]);
  const [pending, setPending] = useState(null);
  const [prompt, setPrompt] = useState("");
  const [signature, setSignature] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const st = await sysGet("/kernel/status");
      setStatus(st);
      setPending(st.pending || null);
      const v = await sysGet("/kernel/versions");
      setVersions(v.versions || []);
    } catch {
      /* kernel unreachable */
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function toggleEnabled() {
    const next = !status.enabled;
    if (next) {
      const ok = await confirm({
        title: "Enable kernel self-update?",
        body: "This lets the agent author changes to the KERNEL — the ring-0 supervisor that " +
          "gates every app change. Changes are validated, held for your approval, and the " +
          "firmware auto-rolls-back a kernel that fails to boot. It is still the highest-risk " +
          "capability here. Enable it?",
        danger: true,
        confirmLabel: "Enable kernel self-update",
      });
      if (!ok) return;
    }
    const { status: code, data } = await sysPost("/config", { patch: { "kernel_update.enabled": next } });
    if (code === 200 && data.ok) {
      toast.ok(next ? "Kernel self-update enabled." : "Kernel self-update disabled.");
      load();
    } else {
      toast.err("Could not change the setting.");
    }
  }

  async function submit() {
    const p = prompt.trim();
    if (!p || busy) return;
    setBusy(true);
    const { data } = await sysPost("/kernel/change_request", { prompt: p });
    setBusy(false);
    if (data.ok && data.pending) {
      setPrompt("");
      toast.ok(`Kernel candidate ${data.short} validated — awaiting your approval below.`);
      load();
    } else {
      toast.err("Kernel change failed: " + (data.reason || "?"));
    }
  }

  async function approve() {
    if (!pending || busy) return;
    if (status.signed_mode && !signature.trim()) {
      toast.err("Signed mode: paste an operator signature over the candidate digest.");
      return;
    }
    const ok = await confirm({
      title: "Approve & promote this kernel?",
      body: `The process will restart. The firmware verifies candidate ${pending.short}, swaps ` +
        "it in, and health-gates it — auto-rolling-back to the current kernel if it won't boot. " +
        "The UI will reconnect once the new (or rolled-back) kernel is serving.",
      danger: true,
      confirmLabel: "Approve & restart",
    });
    if (!ok) return;
    setBusy(true);
    const { data } = await sysPost("/kernel/approve", { sha: pending.sha, signature: signature.trim() });
    setBusy(false);
    if (data.ok) {
      toast.ok("Approved — restarting to apply the new kernel…");
      beginReconnect("Applying the new kernel…");
    } else {
      toast.err("Approve failed: " + (data.reason || "?"));
    }
  }

  async function reject() {
    if (!pending) return;
    const { data } = await sysPost("/kernel/reject", { sha: pending.sha });
    if (data.ok) {
      toast.info("Kernel candidate rejected.");
      setSignature("");
      load();
    } else {
      toast.err("Reject failed: " + (data.reason || "?"));
    }
  }

  async function rollback() {
    const ok = await confirm({
      title: "Roll back the kernel?",
      body: "Point the active kernel back at the previous good version and restart so the " +
        "firmware swaps it back. The UI will reconnect once it's serving.",
      danger: true,
      confirmLabel: "Roll back & restart",
    });
    if (!ok) return;
    const { data } = await sysPost("/kernel/rollback", {});
    if (data.ok) {
      toast.ok("Rolling back the kernel…");
      beginReconnect("Rolling back the kernel…");
    } else {
      toast.err("Rollback failed: " + (data.reason || "?"));
    }
  }

  if (!status) {
    return (
      <Card title="Kernel">
        <span className="muted">loading…</span>
      </Card>
    );
  }

  return (
    <div className="stack">
      <Card title="Kernel self-update">
        <div className="banner warn" style={{ marginTop: 0 }}>
          <strong>⚠ Highest-blast-radius feature.</strong> This lets the agent rewrite the
          <strong> kernel</strong> — the ring-0 supervisor itself. The immutable firmware verifies,
          health-gates, and auto-rolls-back a bad kernel, and every change needs your approval
          below. Keep it off unless you mean to use it.
        </div>
        <label className="check" style={{ marginTop: 10 }}>
          <input type="checkbox" checked={!!status.enabled} onChange={toggleEnabled} />{" "}
          <strong>Enable kernel self-update</strong>
        </label>

        <div className="kernel-facts">
          <div><span className="muted">Active kernel</span>
            <code>{status.active ? digestChip(status.active.digest) : "shipped (image)"}</code>
            {status.in_sync ? <Badge tone="good">in sync</Badge> : <Badge tone="warn">applying…</Badge>}
          </div>
          <div><span className="muted">Shipped digest</span><code>{digestChip(status.shipped_digest)}</code></div>
          <div><span className="muted">Signed mode</span>
            {status.signed_mode
              ? <Badge tone="good">on — signature required to promote</Badge>
              : <Badge tone="muted">off (set KERNEL_INTEGRITY_PUBKEY to require signatures)</Badge>}
          </div>
        </div>
        {status.has_rollback && (
          <div className="row mt">
            <Button variant="danger" onClick={rollback} disabled={busy}>↶ Roll back kernel</Button>
            <span className="field-hint">restore the previous kernel version (restarts the process)</span>
          </div>
        )}
      </Card>

      {status.enabled && !pending && (
        <Card title="Request a kernel change">
          <TextArea rows={4} value={prompt} disabled={busy}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="e.g. Add a structured audit field X to the reboot path — keep every kernel test green." />
          <div className="row mt">
            <Button variant="primary" onClick={submit} disabled={!prompt.trim() || busy}>
              {busy ? "Validating…" : "Validate kernel change"}
            </Button>
            <span className="field-hint">
              validated (syntax + import-smoke + curated kernel tests) then held for approval — never auto-promoted
            </span>
          </div>
        </Card>
      )}

      {pending && (
        <Card title="Kernel change awaiting approval">
          <div className="banner warn" style={{ marginTop: 0 }}>
            Approving <strong>restarts the process</strong>: the firmware swaps in kernel{" "}
            <code>{pending.short}</code> (digest <code>{digestChip(pending.digest)}</code>),
            health-gates it, and auto-rolls-back if it won't boot.
          </div>
          <div className="ver-msg" style={{ marginTop: 8 }}>{pending.message}</div>
          {pending.prompt && <div className="muted" style={{ fontSize: "0.85em" }}>“{pending.prompt}”</div>}
          {status.signed_mode && (
            <label className="field" style={{ marginTop: 10 }}>
              <span className="field-label">Operator signature (ed25519, base64) over the digest</span>
              <TextInput value={signature} onChange={(e) => setSignature(e.target.value)}
                placeholder="paste `python -m bootstrap.integrity sign <privkey_b64>` output for this digest" />
            </label>
          )}
          <div className="row mt">
            <Button variant="primary" onClick={approve} disabled={busy}>✓ Approve & restart</Button>
            <Button variant="danger" onClick={reject} disabled={busy}>Reject</Button>
          </div>
        </Card>
      )}

      <Card title="Kernel version history" actions={<Button onClick={load}>Refresh</Button>}>
        {versions.length === 0 ? (
          <Empty>No kernel versions yet — the store seeds from the shipped kernel on first use.</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr><th>#</th><th>Version</th><th>Message</th><th>Status</th></tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.sha} className={v.is_active ? "is-active" : ""}>
                  <td className="nowrap"><strong>kv{v.seq}</strong></td>
                  <td><code>{v.short}</code> {v.is_active && <Badge tone="accent">active</Badge>}</td>
                  <td><div className="ver-msg">{v.message}</div></td>
                  <td><Badge tone={statusTone(v.status)}>{(v.status || "").replace("_", " ")}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

function statusTone(s) {
  return { promoted: "good", active: "good", approved: "accent", pending: "warn",
    committed: "muted", rejected: "bad", kernel_health_failed: "bad", rolled_back: "warn" }[s] || "";
}
