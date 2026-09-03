import React, { useEffect, useState } from "react";
import { sysGet, apiUrl } from "../api.js";
import { Card, Button, Badge, Empty, Pre, Select, eventTone } from "../components";

// The append-only audit trail: every request, commit, reboot, promote/rollback/revert.
// Records are hash-chained (each carries the previous record's hash), so "Verify chain"
// proves the log wasn't edited in place. Filters run server-side.
const PAGE = 200;

export default function AuditTab() {
  const [rows, setRows] = useState([]); // newest first
  const [hasMore, setHasMore] = useState(false);
  const [raw, setRaw] = useState(false);
  const [eventFilter, setEventFilter] = useState("");
  const [since, setSince] = useState("");
  const [verify, setVerify] = useState(null); // null | {ok, checked, legacy_prefix, first_bad_line}
  const [knownEvents, setKnownEvents] = useState([]);

  function params(offset) {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (eventFilter) p.set("event", eventFilter);
    if (since) p.set("since", since);
    return p.toString();
  }

  async function load() {
    const { audit } = await sysGet("/audit?" + params(0));
    const page = (audit || []).slice().reverse(); // newest first
    setRows(page);
    setHasMore((audit || []).length === PAGE);
    // Keep the filter dropdown stocked with every event name we've seen.
    setKnownEvents((prev) => {
      const s = new Set(prev);
      for (const r of page) if (r.event) s.add(r.event);
      return [...s].sort();
    });
  }
  async function loadMore() {
    const { audit } = await sysGet("/audit?" + params(rows.length));
    const older = (audit || []).slice().reverse();
    setRows((r) => [...r, ...older]);
    setHasMore((audit || []).length === PAGE);
  }
  useEffect(() => {
    load();
  }, [eventFilter, since]);

  async function verifyChain() {
    setVerify({ loading: true });
    try {
      setVerify(await sysGet("/audit/verify"));
    } catch (e) {
      setVerify({ ok: false, error: String(e.message || e) });
    }
  }

  async function download(format) {
    const r = await fetch(apiUrl(`/api/syscall/audit/export?format=${format}&limit=100000`));
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit.${format}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  const rawText = rows
    .map((e) => {
      const rest = Object.fromEntries(
        Object.entries(e).filter(([k]) => !["ts", "event", "prev", "h"].includes(k)),
      );
      return `${e.ts}  ${e.event}  ${JSON.stringify(rest)}`;
    })
    .join("\n");

  function details(e) {
    return Object.entries(e)
      .filter(([k]) => !["ts", "event", "prev", "h"].includes(k)) // chain fields are plumbing
      .map(([k, v]) => (
        <span key={k} className="kv">
          <span className="kv-k">{k}</span>
          <span className="kv-v">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
        </span>
      ));
  }

  return (
    <Card
      title="Audit log"
      actions={
        <>
          <Select
            value={eventFilter}
            onChange={(e) => setEventFilter(e.target.value)}
            options={[{ value: "", label: "all events" }, ...knownEvents.map((ev) => ({ value: ev, label: ev }))]}
          />
          <input
            className="input audit-since"
            type="date"
            value={since}
            onChange={(e) => setSince(e.target.value)}
            title="Only records on/after this date"
          />
          <Button onClick={verifyChain}>Verify chain</Button>
          <Button onClick={() => setRaw((r) => !r)}>{raw ? "Table view" : "Raw"}</Button>
          <Button onClick={() => download("json")}>Export JSON</Button>
          <Button onClick={() => download("csv")}>Export CSV</Button>
          <Button onClick={load}>Refresh</Button>
        </>
      }
    >
      {verify && !verify.loading && (
        <div className={"banner " + (verify.ok ? "ok" : "err")} style={{ marginBottom: 12 }}>
          {verify.ok ? (
            <>
              ✓ hash chain intact — {verify.checked} record{verify.checked === 1 ? "" : "s"} verified
              {verify.legacy_prefix > 0 && ` (${verify.legacy_prefix} pre-chain records skipped)`}
            </>
          ) : (
            <>
              ✗ chain broken at line {verify.first_bad_line ?? "?"}
              {verify.error ? ` — ${verify.error}` : ""} — the log was modified after being written
            </>
          )}
        </div>
      )}
      {rows.length === 0 ? (
        <Empty>No audit entries{eventFilter || since ? " match the filter" : " yet"}.</Empty>
      ) : raw ? (
        <Pre>{rawText}</Pre>
      ) : (
        <table className="table audit">
          <thead>
            <tr>
              <th>Time</th>
              <th>Event</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e, i) => {
              const { tone, icon } = eventTone(e.event);
              return (
                <tr key={e.h || i}>
                  <td className="muted nowrap mono">{(e.ts || "").replace("T", " ").slice(0, 19)}</td>
                  <td>
                    <Badge tone={tone}>
                      {icon} {e.event}
                    </Badge>
                  </td>
                  <td className="audit-details">{details(e)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {hasMore && (
        <div className="row mt" style={{ justifyContent: "center" }}>
          <Button onClick={loadMore}>Load older</Button>
        </div>
      )}
    </Card>
  );
}
