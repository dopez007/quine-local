import React, { useEffect, useMemo, useRef, useState, lazy, Suspense } from "react";
import { appGet, appPost, appDelete, appPut, postStream, getStream, sysGet } from "../api.js";
import {
  Badge,
  Button,
  Empty,
  HarnessMetric,
  HarnessStatusBar,
  HarnessStatusDot,
  Message,
  ToolChip,
} from "../components";
import { useConfirm } from "../components/Confirm.jsx";
import { RUN_STARTERS } from "../templates.js";

// Markdown renderer is code-split into its own chunk (react-markdown + remark/rehype +
// highlight.js are heavy); it loads on the first finalized assistant message.
const Markdown = lazy(() => import("../components/Markdown.jsx"));
const ACTIVE_CONVO_KEY = "quine-run-active-conversation";

// Render assistant markdown, falling back to plain text while the chunk loads.
function Md({ children }) {
  if (!children) return null;
  return (
    <Suspense fallback={<>{children}</>}>
      <Markdown>{children}</Markdown>
    </Suspense>
  );
}

// Reasoning bubble — shown while the model is "thinking" before generating a response.
// Supports both streaming (live) and finalized (collapsible + expandable) modes.
function Reasoning({ text, collapsible = false }) {
  const [open, setOpen] = useState(collapsible ? false : true);
  if (!text) return null;
  const content = (
    <div className="reasoning-text">{text}</div>
  );
  if (collapsible) {
    return (
      <div className="reasoning reasoning-final">
        <button type="button" className="reasoning-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="reasoning-toggle">{open ? '▾' : '▸'}</span>
          <span>Thinking</span>
          <span className="muted reasoning-count">({text.length}c)</span>
        </button>
        {open && content}
      </div>
    );
  }
  return (
    <div className="reasoning">
      <div className="reasoning-head">
        <span className="reasoning-dots">...</span>
        <span>Thinking</span>
      </div>
      {content}
    </div>
  );
}

// ── Artifacts panel (right sidebar) ─────────────────────────────────────────────
function ArtifactsPanel({ onClose }) {
  const [list, setList] = useState([]);
  const [activeTitle, setActiveTitle] = useState(null);
  const [body, setBody] = useState("");
  const [dirty, setDirty] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const editorRef = useRef(null);

  async function loadList() {
    try {
      const { artifacts } = await appGet("/api/artifacts");
      setList(artifacts || []);
    } catch { /* ignore */ }
  }

  async function openArtifact(title) {
    try {
      const a = await appGet("/api/artifacts/" + encodeURIComponent(title));
      setActiveTitle(a.title);
      setBody(a.body);
      setDirty(false);
    } catch { /* ignore */ }
  }

  async function save() {
    if (!activeTitle) return;
    try {
      await appPut("/api/artifacts/" + encodeURIComponent(activeTitle), { body });
      setDirty(false);
      await loadList();
    } catch { /* ignore */ }
  }

  async function remove() {
    if (!activeTitle) return;
    try {
      await appDelete("/api/artifacts/" + encodeURIComponent(activeTitle));
      setActiveTitle(null);
      setBody("");
      setDirty(false);
      await loadList();
    } catch { /* ignore */ }
  }

  async function create() {
    const t = newTitle.trim();
    if (!t) return;
    try {
      await appPut("/api/artifacts/" + encodeURIComponent(t), { body: "" });
      setNewTitle("");
      setCreating(false);
      await loadList();
      await openArtifact(t);
    } catch { /* ignore */ }
  }

  useEffect(() => { loadList(); }, []);

  return (
    <div className="artifacts-panel">
      <div className="artifacts-head">
        <span className="muted">Artifacts</span>
        <div className="row">
          <Button variant="ghost" onClick={() => setCreating(true)}>+ New</Button>
          <Button variant="ghost icon" onClick={onClose} title="Close artifacts panel" aria-label="Close artifacts panel">✕</Button>
        </div>
      </div>

      {creating && (
        <div className="artifacts-create">
          <input
            className="input"
            placeholder="Artifact name…"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            autoFocus
          />
          <div className="row" style={{ marginTop: 6 }}>
            <Button variant="primary" onClick={create}>Create</Button>
            <Button onClick={() => { setCreating(false); setNewTitle(""); }}>Cancel</Button>
          </div>
        </div>
      )}

      <div className="artifacts-list">
        {list.length === 0 && <Empty>No artifacts yet.</Empty>}
        {list.map((a) => (
          <div
            key={a.title}
            className={"artifact-item" + (a.title === activeTitle ? " active" : "")}
            onClick={() => openArtifact(a.title)}
          >
            <span className="artifact-title">{a.title}</span>
            <span className="artifact-meta">{a.chars}c</span>
          </div>
        ))}
      </div>

      {activeTitle && (
        <div className="artifacts-editor">
          <div className="artifacts-editor-head">
            <span className="artifact-title">{activeTitle}</span>
            <div className="row">
              {dirty && <Badge tone="warn">unsaved</Badge>}
              <Button variant="ghost" onClick={save} disabled={!dirty}>Save</Button>
              <Button variant="danger" onClick={remove}>Delete</Button>
            </div>
          </div>
          <textarea
            ref={editorRef}
            className="textarea artifacts-textarea"
            value={body}
            onChange={(e) => { setBody(e.target.value); setDirty(true); }}
            placeholder="Write markdown content here…"
          />
        </div>
      )}
    </div>
  );
}

// ── The Run tab ──────────────────────────────────────────────────────────────────
function messageLabel(role) {
  return {
    assistant: "Agent",
    thinking: "Agent thinking",
    tool_call: "Tool Call",
    tool_result: "Tool Result",
  }[role] || role;
}

// Keep the live transcript in the same shape as the display API. That way a completed run does
// not visually collapse when the client replaces the stream with the persisted conversation.
function appendStreamEntry(entries, ev) {
  const next = [...entries];
  const last = next[next.length - 1];
  if (ev.type === "reasoning") {
    if (last?.role === "thinking") {
      next[next.length - 1] = { ...last, content: last.content + (ev.text || "") };
      return next;
    }
    return [...next, { role: "thinking", content: ev.text || "" }];
  }
  if (ev.type === "token") {
    if (!ev.text) return next;
    if (last?.role === "assistant" && last.streaming) {
      next[next.length - 1] = { ...last, content: last.content + ev.text };
      return next;
    }
    return [...next, { role: "assistant", content: ev.text, streaming: true }];
  }
  if (ev.type === "tool_call") {
    if (last?.role === "assistant" && last.streaming) {
      next[next.length - 1] = { ...last, streaming: false };
    }
    return [...next, { role: "tool_call", name: ev.name, args: ev.args || {} }];
  }
  if (ev.type === "tool_result") {
    return [...next, { role: "tool_result", name: ev.name, status: ev.status || "done" }];
  }
  if (ev.type === "assistant") {
    if (last?.role === "assistant" && last.streaming) {
      const completed = { ...last, content: ev.content || last.content, streaming: false };
      if (ev.usage) completed.usage = ev.usage;
      return [...next.slice(0, -1), completed];
    }
    return [...next, { role: "assistant", content: ev.content || "", usage: ev.usage || null }];
  }
  return next;
}

function chipDetail(tool) {
  const args = tool?.args || {};
  const entries = Object.entries(args)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .slice(0, 3);
  if (!entries.length) return "";
  const detail = entries.map(([key, value]) => `${key}: ${String(value)}`).join(" · ");
  return Object.keys(args).length > entries.length ? `${detail} · ...` : detail;
}

function RunMessage({ message }) {
  const streaming = Boolean(message.streaming);
  return (
    <Message role={message.role} label={messageLabel(message.role)}>
      {message.role === "thinking" && <Reasoning text={message.content} collapsible={!streaming} />}
      {message.role === "tool_call" && (
        <ToolChip name={message.name} detail={chipDetail(message)} />
      )}
      {message.role === "tool_result" && (
        <ToolChip name={message.name} status={message.status} />
      )}
      {(message.role === "user" || message.role === "assistant") && message.content && (
        <div className={"msg-text" + (message.role === "user" ? " plain" : streaming ? " streaming" : "")}>
          {message.role === "user" ? message.content : <Md>{message.content}</Md>}
          {streaming && <span className="caret" />}
        </div>
      )}
      {message.usage && (message.usage.total_tokens || message.usage.prompt_tokens || message.usage.completion_tokens) && (
        <div className="msg-usage-badge" title={
          `Prompt tokens: ${(message.usage.prompt_tokens || 0).toLocaleString()}\n` +
          `Cached prompt: ${(message.usage.cached_tokens || 0).toLocaleString()}\n` +
          `Completion tokens: ${(message.usage.completion_tokens || 0).toLocaleString()}\n` +
          `Total tokens: ${(message.usage.total_tokens || 0).toLocaleString()}`
        }>
          <span className="msg-usage-num">{message.usage.total_tokens || (message.usage.prompt_tokens || 0) + (message.usage.completion_tokens || 0)}</span>
          <span className="msg-usage-label">tokens</span>
        </div>
      )}
    </Message>
  );
}

export default function RunTab({ onNavigate }) {
  const [convos, setConvos] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [streamingId, setStreamingId] = useState(null);
  const [model, setModel] = useState(localStorage.getItem("quine-run-model") || "");
  const [agents, setAgents] = useState([]); // selectable models from config.yaml
  const [live, setLive] = useState(null); // ordered display entries while the agent streams
  const [error, setError] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [artifactsOpen, setArtifactsOpen] = useState(false);
  // A run is in flight in this conversation — true whether we started it or we're only watching
  // (e.g. this tab reloaded mid-run). Drives the activity line and the Stop button, so a run can
  // always be called off and never just sits there looking dead.
  const [runActive, setRunActive] = useState(false);
  const [activity, setActivity] = useState(null); // {phase, seconds, tool, attempt, max, reason}
  const [tick, setTick] = useState(0);            // 1Hz clock, only while a run is in flight
  const runStart = useRef(0);
  const activeIdRef = useRef(null);
  const streamingIdRef = useRef(null);
  const sendingIdsRef = useRef(new Set());
  const abortRefs = useRef(new Map());
  const openRequestRef = useRef(0);
  const scroller = useRef(null);
  const pinnedRef = useRef(true); // is the user scrolled to the bottom of the chat?
  const watchRef = useRef(null); // AbortController for the live-run watch stream (fan-out)
  const confirm = useConfirm();
  const streaming = streamingId !== null && streamingId === activeId;
  const activeConvo = convos.find((c) => c.id === activeId);
  const runElapsed = runStart.current ? Math.floor((Date.now() - runStart.current) / 1000) : 0;
  const tokenTotal = useMemo(
    () => [...messages, ...(live || [])].reduce((total, item) => {
      const usage = item.usage || {};
      return total + (usage.total_tokens || usage.prompt_tokens || 0) +
        (usage.total_tokens ? 0 : (usage.completion_tokens || 0));
    }, 0),
    [messages, live],
  );
  const selectedAgent = agents.find((agent) => agent.model === model);

  async function loadConvos() {
    try {
      const data = await appGet("/api/agent/conversations");
      setConvos(data.conversations || []);
      return data.conversations || [];
    } catch {
      return [];
    }
  }
  async function openConvo(id) {
    const request = ++openRequestRef.current;
    activeIdRef.current = id;
    setActiveId(id);
    localStorage.setItem(ACTIVE_CONVO_KEY, id);
    setError("");
    setMessages([]);
    setLive(null);
    setRunActive(false);
    setActivity(null);
    runStart.current = 0;
    try {
      const c = await appGet("/api/agent/conversations/" + id);
      if (request !== openRequestRef.current || activeIdRef.current !== id) return;
      setMessages(c.messages || []);
      // The agent may still be working after a reload. Pick the run back up so the activity line
      // and Stop button appear straight away.
      setRunActive(!!c.running);
      if (c.running) runStart.current = Date.now();
    } catch {
      if (request !== openRequestRef.current || activeIdRef.current !== id) return;
      setMessages([]);
      setRunActive(false);
    }
  }

  // Tick a 1Hz clock ONLY while a run is in flight — the elapsed seconds in the activity line are
  // the difference between "the agent is working" and "the agent looks frozen".
  useEffect(() => {
    if (!streaming && !runActive) return;
    const iv = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(iv);
  }, [streaming, runActive]);

  function beginRun(id) {
    if (activeIdRef.current !== id) return;
    if (!runStart.current) runStart.current = Date.now();
    setRunActive(true);
    setActivity({ phase: "thinking" });
  }

  function endRun(id) {
    if (activeIdRef.current !== id) return;
    runStart.current = 0;
    setRunActive(false);
    setActivity(null);
  }

  /** Handle the run-liveness frames shared by our own POST stream and the watch stream. */
  function onRunEvent(ev, id) {
    if (activeIdRef.current !== id) return;
    if (ev.type === "status") {
      setActivity({ phase: "waiting_model", seconds: ev.seconds || 0 });
    } else if (ev.type === "retry") {
      setActivity({ phase: "retry", attempt: ev.attempt, max: ev.max, reason: ev.reason });
    } else if (ev.type === "tool_call") {
      setActivity({ phase: "tool", tool: ev.name });
    } else if (ev.type === "token" || ev.type === "reasoning" || ev.type === "tool_result") {
      setActivity({ phase: ev.type === "tool_result" ? "thinking" : "writing" });
    } else if (ev.type === "loop") {
      setError(`The agent kept calling ${ev.tool} with the same arguments — stopped it to avoid a loop.`);
    } else if (ev.type === "stopped") {
      setError("(stopped)");
    }
  }

  /** Elapsed-time + phase label — proof the run is alive, or a clear reason it isn't. */
  function activityLabel() {
    const secs = runElapsed;
    const a = activity || {};
    if (a.phase === "retry") {
      return `No response from the model — retrying (${a.attempt}/${a.max})`;
    }
    if (a.phase === "waiting_model") {
      return `Waiting for the model — ${a.seconds}s with no output`;
    }
    if (a.phase === "tool") return `Running ${a.tool}… · ${secs}s`;
    if (a.phase === "writing") return `Writing the answer… · ${secs}s`;
    if (a.phase === "stopping") return "Stopping the agent…";
    return `Working… · ${secs}s`;
  }
  async function newConvo() {
    try {
      const c = await appPost("/api/agent/conversations", {});
      if (!c?.id) throw new Error(c?.error || "Could not create conversation");
      await loadConvos();
      await openConvo(c.id);
    } catch (e) {
      setError(String(e.message || e));
    }
  }
  async function delConvo(id) {
    const c = convos.find((x) => x.id === id);
    if (c?.running || (id === activeIdRef.current && runActive)) {
      setError("Stop the run before deleting this conversation.");
      return;
    }
    const ok = await confirm({
      title: "Delete conversation",
      body: `Delete "${c?.title || "Untitled"}"? This can't be undone.`,
      danger: true,
      confirmLabel: "Delete",
    });
    if (!ok) return;
    try {
      const result = await appDelete("/api/agent/conversations/" + id);
      if (!result?.ok) throw new Error(result?.error || "Could not delete conversation");
      const list = await loadConvos();
      if (id === activeIdRef.current) {
        localStorage.removeItem(ACTIVE_CONVO_KEY);
        if (list[0]) openConvo(list[0].id);
        else {
          activeIdRef.current = null;
          setActiveId(null);
          setMessages([]);
        }
      }
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  useEffect(() => {
    (async () => {
      try {
        const { config } = await sysGet("/config");
        if (config?.agents) setAgents(config.agents);
        if (!model && config?.agent?.model) setModel(config.agent.model);
      } catch {
        /* ignore */
      }
      const list = await loadConvos();
      const savedId = localStorage.getItem(ACTIVE_CONVO_KEY);
      const restore =
        (savedId && list.find((c) => c.id === savedId)) ||
        list.find((c) => c.running) ||
        list[0];
      if (restore) openConvo(restore.id);
    })();
  }, []);

  useEffect(() => {
    localStorage.setItem("quine-run-model", model || "");
  }, [model]);
  // Autoscroll the chat ONLY when the user is already at the bottom. If they've scrolled
  // up to read, new messages/tokens must not yank the viewport back down.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (!activeId && messages.length === 0 && !live?.length) {
      el.scrollTop = 0;
      return;
    }
    if (pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [activeId, messages, live]);

  function onChatScroll() {
    const el = scroller.current;
    if (el) pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }

  // Close sidebar on narrow screens when a conversation is opened
  useEffect(() => {
    if (window.innerWidth < 720) setSidebarOpen(false);
  }, [activeId]);

  // Follow the active conversation's live agent run. This read-only stream renders an in-flight
  // run as it happens, then reconciles to the saved conversation when it ends. Frames from our own
  // send are ignored here because the POST stream already renders those. The stream stays open
  // across runs until we switch away.
  useEffect(() => {
    const id = activeId;
    if (!id) return;
    const controller = new AbortController();
    watchRef.current = controller;
    const reset = () => setLive(null);
    const append = (ev) => setLive((entries) => appendStreamEntry(entries || [], ev));
    let retryTimer = null;
    let closed = false;
    let firstConnect = true;
    const connect = () => {
      if (!firstConnect) {
        // A reconnect replays the hub buffer from the beginning, so rebuild the live view instead
        // of appending the same tokens and tool calls a second time.
        reset();
        if (activeIdRef.current === id) setLive(null);
      }
      firstConnect = false;
      getStream(
        "/api/agent/conversations/" + id + "/stream",
        (ev) => {
          if (controller.signal.aborted || activeIdRef.current !== id || sendingIdsRef.current.has(id)) return;
          // Any frame here means a run is alive in this conversation — even one we didn't start.
          if (ev.type !== "done") {
            beginRun(id);
            onRunEvent(ev, id);
          }
          if (ev.type === "user") {
            // If we loaded mid-run, discard checkpointed display entries before replaying the
            // hub's authoritative event sequence.
            setMessages((m) => {
              const userIndex = m.map((item) => item.role === "user" && item.content === ev.content)
                .lastIndexOf(true);
              return userIndex < 0
                ? [...m, { role: "user", content: ev.content }]
                : m.slice(0, userIndex + 1);
            });
            reset();
          } else if (["reasoning", "token", "tool_call", "tool_result", "assistant"].includes(ev.type)) {
            append(ev);
          } else if (ev.type === "done") {
            reset();
            setLive(null);
            endRun(id);
            // Reconcile with the authoritative saved conversation (covers any drift, and the
            // sender-disconnected case where no assistant frame arrived).
            appGet("/api/agent/conversations/" + id)
              .then((c) => {
                if (activeIdRef.current === id) setMessages(c.messages || []);
              })
              .catch(() => {});
          } else if (ev.type === "error") {
            setError(ev.error || "error");
          }
        },
        controller.signal,
      ).then(() => {
        if (!closed && !controller.signal.aborted) retryTimer = setTimeout(connect, 1000);
      }).catch(() => {
        if (!closed && !controller.signal.aborted) retryTimer = setTimeout(connect, 1000);
      });
    };
    connect();
    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      controller.abort();
      if (watchRef.current === controller) watchRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // Stop works for ANY viewer of the run — including a tab that reloaded mid-run and is only
  // watching. Ask the server to wind the run down (it saves the partial reply and tells every
  // watcher). An orphaned pre-stream claim ends synchronously, so reconcile it immediately rather
  // than waiting for a `done` frame from a driver task that never existed.
  async function stopRun() {
    const id = activeId;
    if (!id) return;
    setActivity({ phase: "stopping" });
    try {
      const result = await appPost("/api/agent/conversations/" + id + "/stop", {});
      if (result?.running === false && activeIdRef.current === id) {
        setLive(null);
        endRun(id);
        try {
          const c = await appGet("/api/agent/conversations/" + id);
          if (activeIdRef.current === id) setMessages(c.messages || []);
        } catch { /* conversation may have been deleted */ }
        loadConvos();
      }
    } catch {
      // The run may have just finished. Its stream/watch `done` frame remains authoritative.
    }
  }

  // Fill the composer from a starter prompt (the user reviews, then sends).
  function fillStarter(s) {
    setInput(s);
    const ta = document.querySelector(".composer .textarea");
    if (ta) ta.focus();
  }

  async function send() {
    const content = input.trim();
    if (!content || streaming || runActive) return;
    let id = activeId;
    if (!id) {
      try {
        const c = await appPost("/api/agent/conversations", {});
        if (!c?.id) throw new Error(c?.error || "Could not create conversation");
        id = c.id;
        activeIdRef.current = id;
        setActiveId(id);
        localStorage.setItem(ACTIVE_CONVO_KEY, id);
      } catch (e) {
        setError(String(e.message || e));
        return;
      }
    }
    setInput("");
    setError("");
    setMessages((m) => [...m, { role: "user", content }]);
    setStreamingId(id);
    streamingIdRef.current = id;
    beginRun(id);
    sendingIdsRef.current.add(id);
    setLive(null);
    const controller = new AbortController();
    abortRefs.current.set(id, controller);
    try {
      await postStream(
        "/api/agent/conversations/" + id + "/message",
        { content, model },
        (ev) => {
          if (controller.signal.aborted) return;
          if (activeIdRef.current !== id) return;
          onRunEvent(ev, id);
          if (["reasoning", "token", "tool_call", "tool_result", "assistant"].includes(ev.type)) {
            setLive((entries) => appendStreamEntry(entries || [], ev));
          } else if (ev.type === "error") {
            if (activeIdRef.current === id) setError(ev.error || "error");
          }
        },
        controller.signal,
      );
    } catch (e) {
      const busy = String(e.message || e).includes("-> 409");
      if (e.name === "AbortError") {
        if (activeIdRef.current === id) setError("(stopped)");
      } else if (busy) {
        // A run is already streaming in this conversation. Our send never took, so follow the
        // active run through the existing watch stream.
        if (activeIdRef.current === id) {
          setError("A response is already in progress — following the active run.");
        }
      } else {
        if (activeIdRef.current === id) setError(String(e.message || e));
      }
    } finally {
      // Reconcile every path with the backend. The saved display transcript has the same
      // event shape as the live stream, so a refresh or completed run never recombines entries.
      if (id) {
        try {
          const c = await appGet("/api/agent/conversations/" + id);
          if (activeIdRef.current === id) setMessages(c.messages || []);
        } catch { /* conversation may have been deleted */ }
      }
      sendingIdsRef.current.delete(id);
      if (streamingIdRef.current === id) {
        streamingIdRef.current = null;
        setStreamingId(null);
      }
      if (activeIdRef.current === id) {
        setLive(null);
        endRun(id);
      }
      if (abortRefs.current.get(id) === controller) abortRefs.current.delete(id);
      loadConvos();
    }
  }

  const runBusy = streaming || runActive;
  const harnessState = runBusy ? "running" : error === "(stopped)" ? "stopped" : error ? "failed" : "idle";
  const harnessSummary = runBusy
    ? activityLabel()
    : error === "(stopped)"
      ? "The run was stopped; partial work is preserved"
      : error
        ? "The last run needs attention"
        : activeId
          ? `${messages.length} trace entr${messages.length === 1 ? "y" : "ies"} · ready for the next instruction`
          : "Create a session and give the agent a mission";

  return (
    <div className={"run harness-console" + (sidebarOpen ? "" : " sidebar-closed") + (artifactsOpen ? " artifacts-open" : "")}>
      {/* Mobile sidebar toggle */}
      <button
        className="sidebar-toggle"
        onClick={() => setSidebarOpen((o) => !o)}
        title={sidebarOpen ? "Close sidebar" : "Open sidebar"}
        aria-label={sidebarOpen ? "Close conversations sidebar" : "Open conversations sidebar"}
        aria-expanded={sidebarOpen}
      >
        {sidebarOpen ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="18" x2="21" y2="18" />
          </svg>
        )}
      </button>

      <aside className={"convos" + (sidebarOpen ? "" : " hidden")}>
        <div className="convos-head">
          <span className="muted">Conversations</span>
          <Button variant="ghost" onClick={newConvo}>
            + New
          </Button>
        </div>
        <div className="convo-list">
          {convos.length === 0 && <Empty>No conversations yet.</Empty>}
          {convos.map((c) => (
            <div
              key={c.id}
              className={"convo-item" + (c.id === activeId ? " active" : "")}
              role="button"
              tabIndex={0}
              aria-current={c.id === activeId ? "true" : undefined}
              onClick={() => openConvo(c.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  openConvo(c.id);
                }
              }}
            >
              <HarnessStatusDot
                state={c.running || (c.id === activeId && runActive) ? "running" : "idle"}
                label={c.running ? `${c.title || "Untitled"} is running` : `${c.title || "Untitled"} is idle`}
              />
              <div className="convo-main">
                <span className="convo-title">{c.title || "Untitled"}</span>
                <span className="convo-meta">
                  {typeof c.messages === "number" && <span>{c.messages} message{c.messages === 1 ? "" : "s"}</span>}
                </span>
              </div>
              <button
                className="convo-del"
                title={c.running || (c.id === activeId && runActive) ? "Stop the run before deleting" : "Delete"}
                aria-label={"Delete conversation " + (c.title || "Untitled")}
                disabled={c.running || (c.id === activeId && runActive)}
                onClick={(e) => {
                  e.stopPropagation();
                  delConvo(c.id);
                }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </aside>

      <div className="chat harness-stage">
        <HarnessStatusBar
          state={harnessState}
          eyebrow="Run agent · session"
          title={activeConvo?.title || (activeId ? "Untitled session" : "No active session")}
          summary={harnessSummary}
          metrics={
            <>
              <HarnessMetric label="elapsed" value={runBusy ? `${runElapsed}s` : "—"} mono />
              <HarnessMetric
                label="model"
                value={selectedAgent?.name || model || "default"}
                title={model || "Default configured model"}
                className="harness-metric-model"
              />
              <HarnessMetric label="tokens" value={tokenTotal ? tokenTotal.toLocaleString() : "—"} mono />
            </>
          }
          actions={
            <>
              <label className="run-status-model">
                <span>Target model</span>
                <input
                  className="input model-input"
                  placeholder="Pick or type a model"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  list="run-agent-models"
                  title="Choose a configured agent or type any model id"
                />
              </label>
              <datalist id="run-agent-models">
                {agents.map((a) => (
                  <option key={a.model} value={a.model}>{a.name}</option>
                ))}
              </datalist>
              <Button
                variant={artifactsOpen ? "primary" : "ghost"}
                onClick={() => setArtifactsOpen((open) => !open)}
              >
                {artifactsOpen ? "Artifacts open" : "Artifacts"}
              </Button>
              {runBusy && (
                <Button variant="stop" onClick={stopRun}>
                  ■ Stop
                </Button>
              )}
            </>
          }
        >
          {activity?.phase === "retry" && activity.reason && (
            <div className="harness-status-warning" role="status">
              Retry reason: {activity.reason}
            </div>
          )}
        </HarnessStatusBar>

        <div className="chat-scroll harness-trace" ref={scroller} onScroll={onChatScroll}>
          {messages.length === 0 && !live && (
            <div className="run-welcome harness-ready">
              <span className="run-ready-kicker"><HarnessStatusDot state="idle" /> Agent harness ready</span>
              <h2 className="run-welcome-title">Give the agent a <em>mission</em></h2>
              <p className="run-welcome-lead">
                One session can research the web, inspect your knowledge and app, call tools, create
                artifacts, and report every step as it works.
              </p>
              <div className="run-capabilities" aria-label="Agent capabilities">
                <span>Reasoning stream</span><span>Tool execution</span><span>Persistent context</span><span>Artifacts</span>
              </div>
              <div className="starter-grid">
                {RUN_STARTERS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="starter-chip"
                    onClick={() => fillStarter(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
              {onNavigate && (
                <button
                  type="button"
                  className="run-selfmod-cta"
                  onClick={() => onNavigate("modify")}
                >
                  <span className="run-selfmod-text">
                    <strong>Want the app itself to change?</strong> Describe it the way you'd say
                    it, and Quine rebuilds itself — checked before it ships, reversible after.
                  </span>
                  <span className="run-selfmod-go">Open Self-Modify →</span>
                </button>
              )}
            </div>
          )}
          {messages.map((m, i) => <RunMessage key={i} message={m} />)}
          {live?.map((m, i) => <RunMessage key={`${m.role}-${i}`} message={m} />)}
          {(streaming || runActive) && !live?.length && (
            <div className="msg assistant">
              <div className="msg-role">assistant</div>
              <div className="msg-body">
                <div className="thinking-dots">
                  <span className="thinking-dot" /><span className="thinking-dot" /><span className="thinking-dot" />
                </div>
              </div>
            </div>
          )}
          {/* Live proof of work: what the agent is doing right now and for how long. Silence from
              the model is stated outright (and retried) instead of looking like a hang. */}
          {(streaming || runActive) && (
            <div
              className={"run-activity" + (activity?.phase === "retry" ? " warn" : "")}
              data-tick={tick}
              role="status"
              aria-live="polite"
            >
              <span className="run-activity-pulse" />
              <span className="run-activity-label">{activityLabel()}</span>
              {activity?.phase === "retry" && activity.reason && (
                <span className="run-activity-detail">{activity.reason}</span>
              )}
            </div>
          )}
        </div>

        {error && <div className="banner err">{error}</div>}

        <div className="composer">
          <textarea
            className="textarea"
            rows={2}
            placeholder="Give the agent a mission…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            disabled={streaming || runActive}
          />
          <div className="composer-row harness-composer-row">
            <span className="composer-hint">
              {runBusy ? (
                <><HarnessStatusDot state="running" /> Agent is executing this mission. Stop it from the status rail.</>
              ) : (
                <>Enter to send · Shift+Enter for a new line</>
              )}
            </span>
            {!runBusy && (
              <Button variant="primary" onClick={send} disabled={!input.trim()}>
                Run mission →
              </Button>
            )}
          </div>
        </div>
      </div>

      {artifactsOpen && <ArtifactsPanel onClose={() => setArtifactsOpen(false)} />}
    </div>
  );
}
