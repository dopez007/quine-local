import React, { useEffect, useState } from "react";
import { appGet } from "../api.js";
import { Card, Button, Badge, Empty } from "../components";

export default function UsageTab() {
  const [data, setData] = useState(null);
  const [smData, setSmData] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const [d, sm] = await Promise.all([
        appGet("/api/agent/token-usage"),
        appGet("/api/agent/selfmodify-commits"),
      ]);
      setData(d);
      setSmData(sm);
    } catch {
      setData(null);
      setSmData(null);
    }
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  if (loading) {
    return <div className="stack"><Card title="Token Usage"><Empty>Loading…</Empty></Card></div>;
  }

  const hasConversations = data && data.conversations && data.conversations.length > 0;
  const hasCommits = smData && smData.commits && smData.commits.length > 0;
  const hasEmb = (data?.embeddings?.tokens || 0) > 0;

  if (!hasConversations && !hasCommits && !hasEmb) {
    return (
      <Card
        title="Token Usage"
        actions={<Button onClick={load}>Refresh</Button>}
      >
        <Empty>No token usage data yet — send a message in the Run tab or ask the agent to self-modify.</Empty>
      </Card>
    );
  }

  const { totals, conversations } = data || { totals: {}, conversations: [] };
  const smTotals = smData?.totals || {};
  const emb = data?.embeddings || { tokens: 0, cost_usd: 0, calls: 0 };

  const combinedCost = (totals.cost_usd || 0) + (smTotals.cost_usd || 0) + (emb.cost_usd || 0);
  const cachedTotal = (totals.cached_tokens || 0) + (smTotals.cached_tokens || 0);
  const promptTotal = (totals.prompt_tokens || 0) + (smTotals.prompt_tokens || 0);
  const cacheHitPct = promptTotal > 0 ? ((cachedTotal / promptTotal) * 100).toFixed(0) : "0";
  const fmtUsd = (n) => "$" + (n || 0).toFixed((n || 0) >= 1 ? 2 : 4);

  return (
    <div className="stack">
      {/* Summary cards */}
      <div className="usage-summary-grid">
        <div className="usage-summary-card">
          <span className="usage-summary-label">Total Tokens (Chat)</span>
          <span className="usage-summary-value">{totals.total_tokens?.toLocaleString() || 0}</span>
        </div>
        <div className="usage-summary-card">
          <span className="usage-summary-label">Self-Modify Tokens</span>
          <span className="usage-summary-value">{smTotals.total_tokens?.toLocaleString() || 0}</span>
        </div>
        <div className="usage-summary-card">
          <span className="usage-summary-label">Combined Total</span>
          <span className="usage-summary-value">
            {((totals.total_tokens || 0) + (smTotals.total_tokens || 0) + (emb.tokens || 0)).toLocaleString()}
          </span>
        </div>
        <div className="usage-summary-card">
          <span className="usage-summary-label">Embedding Tokens</span>
          <span className="usage-summary-value" title={`${emb.calls} embed call(s) · ${fmtUsd(emb.cost_usd)}`}>
            {(emb.tokens || 0).toLocaleString()}
          </span>
        </div>
        <div className="usage-summary-card">
          <span className="usage-summary-label">Self-Modify Commits</span>
          <span className="usage-summary-value">{smTotals.commits || 0}</span>
        </div>
        <div className="usage-summary-card">
          <span className="usage-summary-label">Est. Cost (Combined)</span>
          <span className="usage-summary-value" title="Estimated — edit pricing in backend_config.json">
            {fmtUsd(combinedCost)}
          </span>
        </div>
        <div className="usage-summary-card">
          <span className="usage-summary-label">Cache Hit</span>
          <span className="usage-summary-value" title={`${cachedTotal.toLocaleString()} cached prompt tokens`}>
            {cacheHitPct}%
          </span>
        </div>
      </div>

      {/* Self-Modify Commits */}
      {hasCommits ? (
        <Card
          title="Self-Modify Commits"
          actions={<Button onClick={load}>Refresh</Button>}
        >
          <table className="table usage-table">
            <thead>
              <tr>
                <th>Commit</th>
                <th>Date</th>
                <th className="num">Prompt</th>
                <th className="num">Cached</th>
                <th className="num">Completion</th>
                <th className="num">Total</th>
                <th className="num">Est. $</th>
              </tr>
            </thead>
            <tbody>
              {smData.commits.slice().reverse().map((c) => (
                <tr key={c.id}>
                  <td className="usage-convo-title" title={c.message}>
                    {c.message || "(no message)"}
                  </td>
                  <td className="muted" style={{ whiteSpace: "nowrap", fontSize: 12 }}>
                    {new Date(c.timestamp * 1000).toLocaleString()}
                  </td>
                  <td className="num">{c.usage?.prompt_tokens?.toLocaleString() || 0}</td>
                  <td className="num">{(c.usage?.cached_tokens || 0).toLocaleString()}</td>
                  <td className="num">{c.usage?.completion_tokens?.toLocaleString() || 0}</td>
                  <td className="num">
                    <Badge tone="accent">{(c.usage?.total_tokens || 0).toLocaleString()}</Badge>
                  </td>
                  <td className="num">{fmtUsd(c.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td><strong>Total ({smTotals.commits || 0} commits)</strong></td>
                <td />
                <td className="num"><strong>{(smTotals.prompt_tokens || 0).toLocaleString()}</strong></td>
                <td className="num"><strong>{(smTotals.cached_tokens || 0).toLocaleString()}</strong></td>
                <td className="num"><strong>{(smTotals.completion_tokens || 0).toLocaleString()}</strong></td>
                <td className="num"><Badge tone="accent"><strong>{(smTotals.total_tokens || 0).toLocaleString()}</strong></Badge></td>
                <td className="num"><strong>{fmtUsd(smTotals.cost_usd)}</strong></td>
              </tr>
            </tfoot>
          </table>
        </Card>
      ) : (
        <Card title="Self-Modify Commits" actions={<Button onClick={load}>Refresh</Button>}>
          <Empty>No self-modify commits yet — ask the agent to make changes in the Self-Modify tab.</Empty>
        </Card>
      )}

      {/* Per-conversation breakdown */}
      {hasConversations && (
        <Card title="Chat Per-Conversation Breakdown">
          <table className="table usage-table">
            <thead>
              <tr>
                <th>Conversation</th>
                <th className="num">Rounds</th>
                <th className="num">Prompt</th>
                <th className="num">Cached</th>
                <th className="num">Completion</th>
                <th className="num">Total</th>
                <th className="num">Est. $</th>
              </tr>
            </thead>
            <tbody>
              {conversations.map((c) => (
                <tr key={c.id}>
                  <td className="usage-convo-title">{c.title}</td>
                  <td className="num">{c.rounds}</td>
                  <td className="num">{c.prompt_tokens?.toLocaleString() || 0}</td>
                  <td className="num">{(c.cached_tokens || 0).toLocaleString()}</td>
                  <td className="num">{c.completion_tokens?.toLocaleString() || 0}</td>
                  <td className="num">
                    <Badge>{(c.total_tokens || c.prompt_tokens + c.completion_tokens || 0).toLocaleString()}</Badge>
                  </td>
                  <td className="num">{fmtUsd(c.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td><strong>Total</strong></td>
                <td className="num">{conversations.reduce((s, c) => s + c.rounds, 0)}</td>
                <td className="num"><strong>{(totals.prompt_tokens || 0).toLocaleString()}</strong></td>
                <td className="num"><strong>{(totals.cached_tokens || 0).toLocaleString()}</strong></td>
                <td className="num"><strong>{(totals.completion_tokens || 0).toLocaleString()}</strong></td>
                <td className="num"><Badge tone="accent"><strong>{(totals.total_tokens || 0).toLocaleString()}</strong></Badge></td>
                <td className="num"><strong>{fmtUsd(totals.cost_usd)}</strong></td>
              </tr>
            </tfoot>
          </table>
        </Card>
      )}

      {/* Simple bar visualization for conversations */}
      {hasConversations && conversations.length > 0 && (
        <Card title="Chat Token Distribution">
          <div className="usage-bars">
            {conversations.map((c) => {
              const pct = totals.total_tokens > 0
                ? ((c.total_tokens || 0) / totals.total_tokens * 100).toFixed(1)
                : "0";
              return (
                <div key={c.id} className="usage-bar-row">
                  <span className="usage-bar-label" title={c.title}>{c.title}</span>
                  <div className="usage-bar-track">
                    <div
                      className="usage-bar-fill"
                      style={{ width: pct + "%" }}
                    />
                  </div>
                  <span className="usage-bar-pct">{pct}%</span>
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {/* Self-modify commit visualization */}
      {hasCommits && (
        <Card title="Self-Modify Token Distribution Per Commit">
          <div className="usage-bars">
            {smData.commits.slice().reverse().map((c) => {
              const totalTokens = smTotals.total_tokens || 1;
              const pct = ((c.usage?.total_tokens || 0) / totalTokens * 100).toFixed(1);
              return (
                <div key={c.id} className="usage-bar-row">
                  <span className="usage-bar-label" title={c.message}>
                    {(c.message || "commit").slice(0, 30)}
                  </span>
                  <div className="usage-bar-track">
                    <div
                      className="usage-bar-fill"
                      style={{ width: pct + "%", background: "var(--grad-brand)" }}
                    />
                  </div>
                  <span className="usage-bar-pct">{pct}%</span>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}
