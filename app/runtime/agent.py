"""The agent loop. EDIT THIS FREELY to change how the agent operates — add logging,
capture token usage, run subagents, change retry behavior. It is plain code in the
versioned image; a bad change is caught by validation or rolled back.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import time
import uuid

from . import sdk
from . import tools as toolmod
from .engines import LiteLLMEngine, ScriptedEngine
from .prompt import REVIEW_PROMPT, SYSTEM_PROMPT

# ── token-saver constants ──────────────────────────────────────────────────────────
_MAX_TOOL_RESULT = 120000    # chars per tool result in history (high ceiling — see _compress_content)
# Per-string-value ceiling for stored tool-call ARGUMENTS. Deliberately huge: the arguments
# are the agent's only record of WHAT it just did (the full content of every write_file /
# edit_file). Gutting them (the old 500-char cap) was the root cause of the agent redoing
# edits and "forgetting what it had done". Only a pathological multi-tens-of-KB single value
# is ever shortened, and even then the stored JSON stays valid (no dangling "…").
_MAX_TOOL_ARG_VALUE = 60000
_MAX_THOUGHT = 1200          # chars for thought event payload
_STEER_POLL_INTERVAL = 0.5   # seconds between steering polls when idle
# Optional review pass: cap how many times review→fix can cycle within one run, so a picky
# reviewer can never wedge a valid change (and it stays bounded inside max_steps).
_MAX_REVIEW_ROUNDS = 2
_MAX_REVIEW_DIFF = 60000     # chars of staged diff handed to the reviewer
# Generous safety bound on restored (uncommitted) history — the commit-scoped reset is the
# real fix; this only trips in pathological nudge chains so context can't grow unbounded.
_MAX_RESTORE_MSGS = 40
# Higher ceiling for a "continue from a commit" resume: the seeded transcript is a full committed
# conversation, not a short nudge chain, so we carry substantially more of it as context.
_MAX_RESUME_MSGS = 200


def _layout() -> str:
    return "\n".join(
        sorted(p.name + ("/" if p.is_dir() else "") for p in sdk.STAGING.iterdir() if p.name != ".git")
    )


def _oneline(s: str) -> str:
    return " ".join((s or "").split())


# Arg keys that carry file/source content. They must NEVER reach the live event stream (a user
# watching a self-mod run must not be able to read the app's own code off the wire), so we replace
# their values with a size hint. write_file/edit_file are the vectors this closes.
_CONTENT_ARG_KEYS = frozenset(
    {
        "body",
        "code",
        "content",
        "data",
        "new",
        "new_str",
        "old",
        "old_str",
        "patch",
        "source",
        "text",
    }
)


def _safe_args_preview(args: dict, limit: int = 160) -> str:
    """A SOURCE-FREE rendering of tool-call args for the live log. Content-bearing values (file
    bodies, edit strings) become a `<N chars>` placeholder; safe keys (path/message/query) stay so
    the step still says what the agent is doing. The model's own history keeps the real args via
    `_compress_tool_args` — this only affects what the UI can see."""
    if not isinstance(args, dict):
        return f"<{len(str(args))} chars>"
    safe: dict = {}
    for k, v in args.items():
        if k in _CONTENT_ARG_KEYS and isinstance(v, str):
            safe[k] = f"<{len(v)} chars>"
        elif isinstance(v, str) and len(v) > 80:
            safe[k] = v[:80] + f"…<+{len(v) - 80}>"
        else:
            safe[k] = v
    try:
        s = json.dumps(safe, ensure_ascii=False)
    except Exception:
        s = str(safe)
    return s if len(s) <= limit else s[:limit] + "…"


def _compress_content(text: str, limit: int = _MAX_TOOL_RESULT) -> str:
    """Truncate long results and add a summary note."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[+{len(text) - limit} chars truncated]"


def _compress_tool_args(args: dict) -> str:
    """Serialise tool-call arguments to a VALID JSON string for the message history.

    Kept FAITHFUL (not gutted): the model must be able to re-read the full content it just
    wrote, or it loses track and repeats edits. Only an individual string value larger than
    _MAX_TOOL_ARG_VALUE is shortened, and even then the result stays valid JSON — never a
    dangling "…" that would corrupt the replayed assistant tool_call.
    """
    if not isinstance(args, dict):
        try:
            return json.dumps(args, ensure_ascii=False)
        except Exception:
            return json.dumps({"_unserialisable": str(args)[:_MAX_TOOL_ARG_VALUE]})
    safe: dict = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > _MAX_TOOL_ARG_VALUE:
            kept = v[:_MAX_TOOL_ARG_VALUE]
            safe[k] = kept + f"\n…[+{len(v) - len(kept)} chars omitted from history; the full value was written to disk]"
        else:
            safe[k] = v
    try:
        return json.dumps(safe, ensure_ascii=False)
    except Exception:
        return json.dumps({"_unserialisable": str(args)[:_MAX_TOOL_ARG_VALUE]})


def _make_engine(config: dict, prompt: str):
    agent = config.get("agent", {})
    if agent.get("engine") == "scripted":
        return ScriptedEngine(prompt)
    return LiteLLMEngine(agent.get("model", "gpt-4o-mini"), float(agent.get("temperature", 0.0)))


def _data_dir() -> pathlib.Path:
    data_dir = pathlib.Path(sdk.DATA_DIR) if sdk.DATA_DIR else sdk.STAGING / ".data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# ── Optional review pass (agent.review_enabled) ─────────────────────────────────────
def _staged_diff(max_chars: int = _MAX_REVIEW_DIFF, staging_dir: str | None = None) -> str:
    """Unified diff of the agent's UNCOMMITTED edits in staging (adds + mods + dels).

    Stages everything, captures `git diff --cached`, then unstages again so the working tree
    and index are left exactly as we found them (the kernel does its own `git add -A` at commit
    time). Best-effort: returns "" if git isn't available or the tree isn't a repo."""
    staging = staging_dir or str(sdk.STAGING)

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=staging, capture_output=True,
                              text=True, timeout=60)

    try:
        _git("add", "-A")
        try:
            diff = _git("diff", "--cached", "--no-color").stdout or ""
        finally:
            _git("reset", "-q")  # restore the pre-review (unstaged) state
    except Exception as exc:  # git missing / not a repo / timeout — review just gets no diff
        sdk.step("review_error", summary=f"could not compute staged diff: {exc}")
        return ""
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n…[+{len(diff) - max_chars} chars truncated]"
    return diff


def _parse_review_verdict(content: str) -> tuple[bool, str]:
    """Interpret a reviewer reply → (approved, findings). The reviewer is asked to put
    APPROVE or REQUEST_CHANGES on the first line; anything that is not an explicit APPROVE is
    treated as changes-requested (conservative — bounded by _MAX_REVIEW_ROUNDS)."""
    content = (content or "").strip()
    if not content:
        return True, ""  # empty reply ⇒ don't block a validated change
    first = content.splitlines()[0].strip().upper()
    if first.startswith("APPROVE"):
        return True, ""
    return False, content


def _review_staging(prompt: str, agent_cfg: dict) -> tuple[bool, str]:
    """Run one review pass over the staged diff. Returns (approved, findings).

    Fails OPEN: any error (no diff, no model, syscall failure, malformed response) returns
    approved=True so the review never blocks an otherwise-valid, validated change."""
    diff = _staged_diff()
    if not diff.strip():
        return True, ""
    model = (agent_cfg.get("review_model") or "").strip() or agent_cfg.get("model", "")
    if not model:
        return True, ""
    messages = [
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user",
         "content": f"ORIGINAL REQUEST:\n{prompt}\n\nSTAGED DIFF (the change to review):\n{diff}"},
    ]
    try:
        resp = sdk.llm_call(model, messages, temperature=0.0)
    except Exception as exc:
        sdk.step("review_error", summary=f"review call failed: {exc}")
        return True, ""
    if not isinstance(resp, dict) or not resp.get("ok"):
        err = (resp or {}).get("error", "unknown") if isinstance(resp, dict) else "unknown"
        sdk.step("review_error", summary=f"review call failed: {err}")
        return True, ""
    try:
        content = resp["response"]["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return True, ""
    return _parse_review_verdict(content)


def _convo_path() -> pathlib.Path:
    """Path to the persisted conversation file for this NUDGE SESSION.

    Uses a STABLE name (not tied to TASK_ID) so that steering/nudge messages
    from a prior agent run are found on the next run — preserving the full
    conversation context across reboots and task boundaries.
    """
    return _data_dir() / "agent_conversation.jsonl"


def _convo_snapshot_path(task_id: str) -> pathlib.Path:
    """Durable, task-keyed copy of a committed conversation, so a specific version can be
    re-opened later ("continue from a commit"). The registry links a version's sha → its
    task_id, so `data/selfmod_convos/<task_id>.jsonl` is addressable from any version."""
    d = _data_dir() / "selfmod_convos"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_id}.jsonl"


def _snapshot_convo(task_id: str, messages: list[dict]) -> None:
    """Persist the full message list under a task-keyed name at commit time. Unlike the
    volatile active convo (archived on the next cycle), this copy is never overwritten — it
    is the transcript a future "continue from this version" run resumes from. Best-effort."""
    if not task_id:
        return
    try:
        with _convo_snapshot_path(task_id).open("w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except Exception:
        pass  # best-effort — a failed snapshot must never break the commit


def _save_messages(messages: list[dict]) -> None:
    """Append new messages since last save to the conversation log (append-only JSONL)."""
    p = _convo_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    except Exception:
        pass  # best-effort


def _load_history() -> list[dict]:
    """Load previously persisted messages (system prompt is skipped — rebuilt fresh)."""
    p = _convo_path()
    if not p.exists():
        return []
    msgs = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        return []
    return msgs


def _session_path() -> pathlib.Path:
    data_dir = pathlib.Path(sdk.DATA_DIR) if sdk.DATA_DIR else sdk.STAGING / ".data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "agent_session.json"


def _read_session() -> dict:
    p = _session_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_session(sess: dict) -> None:
    try:
        _session_path().write_text(json.dumps(sess), encoding="utf-8")
    except Exception:
        pass


def _archive_convo() -> None:
    """Retire the current conversation log — kept as a timestamped archive (not deleted),
    so a committed cycle's transcript is preserved but never replayed into the next run."""
    p = _convo_path()
    if p.exists():
        try:
            p.rename(p.with_name(f"agent_conversation.{int(time.time())}.jsonl"))
        except Exception:
            try:
                p.unlink()
            except Exception:
                pass


def _maybe_seed_resume() -> bool:
    """"Continue from a commit": if the kernel set QUINE_RESUME_CONVO, prime this fresh cycle's
    active conversation with that version's saved transcript so the run continues it with full
    context. Call ONLY at the start of a fresh cycle (after _archive_convo cleared the active
    file). Returns True if a snapshot was seeded."""
    src = (sdk.RESUME_CONVO or "").strip()
    if not src:
        return False
    p = pathlib.Path(src)
    if not p.exists():
        return False
    try:
        shutil.copyfile(p, _convo_path())
    except Exception:
        return False
    n = 0
    try:
        n = sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except Exception:
        pass
    sdk.step("resume_seeded", summary=f"continuing a prior version — seeded {n} messages from its transcript")
    return True


def _bound_history(history: list[dict], cap: int = _MAX_RESTORE_MSGS) -> list[dict]:
    """Cap carried history to the last `cap` messages, then drop any leading `tool` messages so
    the restored slice never starts with a tool result whose preceding assistant tool_call was
    trimmed away (which providers reject)."""
    if len(history) > cap:
        history = history[-cap:]
    while history and history[0].get("role") == "tool":
        history = history[1:]
    return history


def _record_commit(message: str, usage: dict) -> None:
    """Persist commit + token-usage record under DATA_DIR."""
    try:
        data_dir = pathlib.Path(sdk.DATA_DIR) if sdk.DATA_DIR else sdk.STAGING / ".data"
        data_dir.mkdir(parents=True, exist_ok=True)
        commits_file = data_dir / "selfmodify_commits.json"
        commits = []
        if commits_file.exists():
            try:
                commits = json.loads(commits_file.read_text(encoding="utf-8"))
            except Exception:
                commits = []
        entry = {
            "id": "sm_" + uuid.uuid4().hex[:8],
            "timestamp": time.time(),
            "message": message,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cached_tokens": usage.get("cached_tokens", 0),
            },
        }
        commits.append(entry)
        commits_file.write_text(json.dumps(commits, indent=2), encoding="utf-8")
    except Exception:
        pass


def run() -> None:
    config = sdk.CONFIG
    agent_cfg = config.get("agent", {})
    prompt = sdk.PROMPT
    max_steps = int(agent_cfg.get("max_steps", 40))
    engine = _make_engine(config, prompt)

    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    nudged = False
    review_enabled = bool(agent_cfg.get("review_enabled")) and agent_cfg.get("engine") != "scripted"
    review_rounds = 0

    # ── Commit-scoped conversation: a self-mod conversation lives for ONE commit cycle.
    # If the previous cycle committed (or there is no session), retire its transcript and
    # start fresh — so a new request never replays past committed tasks (the big token
    # leak). While a cycle is uncommitted the conversation is restored so nudges continue.
    resumed = False
    session = _read_session()
    if (not session) or session.get("committed"):
        _archive_convo()
        # "Continue from a commit": seed the fresh cycle with the chosen version's transcript
        # (no-op for a normal request). Must run AFTER _archive_convo cleared the active file.
        resumed = _maybe_seed_resume()
        session = {"active": True, "committed": False, "started": time.time()}
        _write_session(session)

    # ── Reconstruct conversation: fresh system + user prompt, then restored history ──
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(layout=_layout())},
        {"role": "user", "content": prompt},
    ]

    history = _load_history()
    if history:
        # Strip any old system prompt from history so we only keep user/assistant/tool
        history = [m for m in history if m.get("role") != "system"]
        # Deduplicate: if the first user message matches the current prompt, skip restoring
        # the initial user message from history (we already added it fresh)
        if history and history[0].get("role") == "user" and history[0].get("content") == prompt:
            history = history[1:]
        # Safety cap so context can't grow unbounded. A resumed version transcript is a full
        # committed conversation (legitimately long), so allow a higher ceiling than the tight
        # nudge-chain bound.
        history = _bound_history(history, _MAX_RESUME_MSGS if resumed else _MAX_RESTORE_MSGS)
        if history:
            # Add a continuation marker so the model knows this is a resumed session
            messages.append({
                "role": "system",
                "content": "[session resumed — the previous agent run ended. "
                           "Continue the conversation from exactly where it left off below. "
                           "All prior context, tool results, and decisions are preserved.]"
            })
        messages.extend(history)
        sdk.step("restored", summary=f"restored {len(history)} prior messages from convo log")

    # ── Pre-run steering: pick up any nudge messages queued while we were stopped ──
    pre_steer = sdk.poll_steer()
    for msg in pre_steer:
        sdk.step("steer_received", summary=msg[:160])
        steer_entry = {"role": "user", "content": "[steering] " + msg}
        messages.append(steer_entry)
        _save_messages([steer_entry])
    if pre_steer:
        sdk.step("pre_steer", summary=f"loaded {len(pre_steer)} queued steering messages")

    # Write the initial messages to the convo log (so a nudge without history is ok)
    _save_messages(messages)

    # ── Error-tracker notice (appended AFTER the save so it's never persisted/replayed:
    # it is recomputed fresh each run). One line, so the agent can't miss live errors —
    # especially regressions its own previous commit caused. Skipped under the scripted
    # engine: that is the deterministic test substrate, and a conditional extra
    # step/message would make scripted step counts depend on ambient recorded errors.
    if agent_cfg.get("engine") != "scripted":
        try:
            err_groups, err_active = toolmod.unresolved_error_summary()
        except Exception:
            err_groups = err_active = 0
        if err_groups:
            messages.append({
                "role": "system",
                "content": f"[error tracker] {err_groups} unresolved error group(s)"
                           + (f", {err_active} seen in the active version" if err_active else "")
                           + " — call get_errors for details before/while making changes."})
            sdk.step("errors_notice", summary=f"{err_groups} unresolved error group(s)")

    sdk.step("start", summary=f"engine={agent_cfg.get('engine')} model={agent_cfg.get('model')} steps={len(messages)}")

    for step_i in range(max_steps):
        # ── Mid-run steering (follow-up nudges) ────────────────────────────────
        steer_msgs = sdk.poll_steer()
        for msg in steer_msgs:
            sdk.step("steer_received", summary=msg[:160])
            steer_entry = {"role": "user", "content": "[steering] " + msg}
            messages.append(steer_entry)
            _save_messages([steer_entry])

        try:
            result = engine.step(messages, toolmod.TOOL_SCHEMAS)
            text, calls, reasoning = result[0], result[1], result[2]
            step_usage = result[3] if len(result) > 3 else {}
        except Exception as exc:
            sdk.step("engine_error", summary=f"error: {exc}")
            return

        # Accumulate token usage
        if step_usage:
            token_usage["prompt_tokens"] += step_usage.get("prompt_tokens", 0)
            token_usage["completion_tokens"] += step_usage.get("completion_tokens", 0)
            token_usage["cached_tokens"] += step_usage.get("cached_tokens", 0)
            token_usage["total_tokens"] = token_usage["prompt_tokens"] + token_usage["completion_tokens"]

        # ── Thought event (compressed) ─────────────────────────────────────────
        thought = (reasoning or text or "").strip()
        if thought:
            sdk.step("thought", summary=_oneline(thought)[:200], thought=thought[:_MAX_THOUGHT])

        names = ",".join(c["name"] for c in calls)
        sdk.step("assistant", summary=((text or "").strip() + " " if text else "") + (f"→ {names}" if names else ""))

        if not calls:
            if not nudged:
                nudged = True
                sdk.step("end_no_tools",
                         summary="no tools called — waiting for steering (nudge: propose_commit now)")
                # Poll for steering more responsively before looping
                import time as _time
                _time.sleep(_STEER_POLL_INTERVAL)
                continue
            else:
                sdk.step("end_no_tools",
                         summary="still no tools after nudge — stopping")
                return

        # ── Append assistant message (compressed args) ─────────────────────────
        assistant_entry = {
            "role": "assistant", "content": text or "",
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": _compress_tool_args(c["args"])}}
                for c in calls
            ],
        }
        messages.append(assistant_entry)
        _save_messages([assistant_entry])

        for c in calls:
            _preview = _safe_args_preview(c["args"])
            sdk.step("tool_call", name=c["name"], args=_preview,
                     summary=f"{c['name']}({_preview})")

            # ── Malformed/truncated tool-call arguments: tell the model WHY, instead of
            #    running the tool with empty args and handing back a cryptic "missing
            #    argument". This is what turns the silent {} fallback into a fixable signal.
            if c.get("args_error"):
                hint = (
                    f"ERROR: the arguments for `{c['name']}` could not be parsed: "
                    f"{c['args_error']}. This almost always means the call was cut off "
                    f"because the response hit the output-token limit (the raw arguments "
                    f"were {c.get('raw_args_len', 0)} chars). Re-send this call with a "
                    f"SMALLER payload: for a large file, make several targeted edit_file "
                    f"patches instead of writing the whole file in one call."
                )
                sdk.step("tool_result", name=c["name"], summary=f"{c['name']}: malformed arguments")
                tool_entry = {"role": "tool", "tool_call_id": c["id"], "content": hint}
                messages.append(tool_entry)
                _save_messages([tool_entry])
                continue

            if c["name"] == "propose_commit":
                res = sdk.validate()
                if res.get("ok"):
                    # ── Optional review pass: an independent LLM inspects the staged diff
                    # (already syntax/import/build-valid) for logic bugs, regressions, and
                    # unmet requirements. Findings go back to THIS agent to fix (reusing the
                    # same validate-fail retry path below); bounded by _MAX_REVIEW_ROUNDS.
                    if review_enabled and review_rounds < _MAX_REVIEW_ROUNDS:
                        review_rounds += 1
                        sdk.step("review_start",
                                 summary=f"reviewing staged changes (round {review_rounds})")
                        approved, findings = _review_staging(prompt, agent_cfg)
                        if not approved:
                            sdk.step("review_issues",
                                     summary="review requested changes: " + _oneline(findings)[:160])
                            review_entry = {
                                "role": "tool", "tool_call_id": c["id"],
                                "content": ("REVIEW FOUND ISSUES — do NOT re-propose until these "
                                            "are addressed:\n" + findings + "\n\nApply the fixes "
                                            "above, then call propose_commit again."),
                            }
                            messages.append(review_entry)
                            _save_messages([review_entry])
                            continue  # hand control back to the agent to fix the findings
                        sdk.step("review_pass", summary="review approved — finalizing")
                    msg = c["args"].get("message", "agent change")
                    total = token_usage.get("total_tokens", 0)
                    _record_commit(msg, token_usage)
                    # Close this conversation cycle: the NEXT self-mod request starts fresh
                    # (this is what stops new requests replaying the committed transcript).
                    session["committed"] = True
                    _write_session(session)
                    sdk.step("committed", summary=f"commit recorded ({total} tokens)")
                    # Append final tool result (don't persist — we're done)
                    messages.append({"role": "tool", "tool_call_id": c["id"],
                                     "content": "validation passed; committing"})
                    # Durable, task-keyed snapshot of the FULL, well-formed transcript (taken
                    # AFTER the final tool result so every tool_call is resolved — a future
                    # "continue from this version" run resumes it and providers reject a dangling
                    # assistant tool_call). The registry links the resulting sha → this task_id.
                    _snapshot_convo(sdk.TASK_ID, messages)
                    # Keep the convo file so the NEXT run (after reboot or steering)
                    # can pick up the conversation and continue where we left off.
                    # The commit marker will tell the restored run that work was done.
                    sdk.propose(msg)
                    return
                result_content = "VALIDATION FAILED:\n" + res.get("report", "")
                tool_entry = {"role": "tool", "tool_call_id": c["id"], "content": result_content}
                messages.append(tool_entry)
                _save_messages([tool_entry])
            else:
                # ── Execute tool & compress result ─────────────────────────────
                raw = toolmod._run_one(c["name"], c["args"])
                # Content-free status only: a tool result is the app's own source (read_file,
                # search, shell output), so we surface size — never the text — to the live log.
                _lines = raw.count("\n") + 1 if raw else 0
                sdk.step("tool_result", name=c["name"],
                         summary=f"{c['name']}: done ({len(raw)} chars, {_lines} lines)")
                compressed = _compress_content(raw)
                tool_entry = {"role": "tool", "tool_call_id": c["id"], "content": compressed}
                messages.append(tool_entry)
                _save_messages([tool_entry])
