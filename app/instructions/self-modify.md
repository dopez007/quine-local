---
title: Self-Modify
category: Evolving the app
order: 40
---
# Self-Modify

This is the superpower: **describe a change and the agent rewrites this app into it.**

## The safe loop

1. **You request** a change (type it, or click a one-click **template**).
2. The agent **edits a staging copy** of the whole system image — backend, frontend, even
   its own runtime.
3. The change is **validated**: Python syntax + import + structure checks, *and* the React
   frontend is rebuilt. If validation fails, the agent fixes it and retries.
4. **Preview (optional):** turn on *"Preview changes before they go live"* to hold the
   change for review. You then **Diff / Approve / Reject** it in the **Versions** tab.
5. **Reboot** into the new version. If the new version fails to boot, the kernel
   **auto-rolls-back** to the last good one.

## Advisor suggestions

The **Advisor** panel at the top is the system noticing its own problems. Hit **Analyze
now** and it mines the harness's telemetry — unresolved errors, versions that failed their
health or verification gates, the audit trail, model spend — and proposes concrete
improvements, each with a rationale, cited evidence, and a ready-to-run change request.

- **Run** submits the proposal through the exact same safe loop above (validation, health
  gate, your approval setting — nothing is bypassed).
- **Edit first** drops the proposal's prompt into the request box so you can steer it.
- **Schedule…** runs it once at a set time (UTC) — under the hood this creates a one-shot
  entry in **Settings ▸ Automation**, so it obeys the automation master switch, the daily
  cap, and the hold-for-approval rules. Cancel it any time before it fires.
- **Dismiss** hides a suggestion; the same finding won't be re-proposed for a couple of
  weeks.

The **Auto-analyze** setting makes the Advisor mine the telemetry on a clock (every 6/12/24
hours) so fresh suggestions are waiting for you — analysis only ever produces suggestions,
never runs one. You can also ask about suggestions in the **Run** chat ("what does the
advisor suggest?").

Want the **full loop with zero clicks**? Create an **Advisor auto-file** automation in
**Settings ▸ Automation**: the system then files its own open suggestions as change
requests (one per pass, each suggestion at most once a month), still under every safety
rail — the automation master switch, the daily cap, and hold-for-approval. Combined with
Auto-analyze, the harness notices, proposes, implements, verifies — and the finished
change waits for your approval.

Suggestions are only text until you act on one (or an automation you configured acts) —
the Advisor itself never changes code.

## Agent evals

The safety net for the agent's **own brain**. When a change touches the agent's runtime
(its loop, tools, prompt), passing the app's health check isn't proof enough — the *agent*
could have gotten worse. Turn on **Agent evals** and define a few benchmark tasks (small,
representative change requests): before any runtime change ships, the candidate must
complete every task **using its own new runtime** and pass validation. A candidate that
breaks its own brain is rejected (`eval failed` in Versions) while the running version
stays untouched. **Benchmark current version** runs the suite on demand as a baseline.
Benchmark tasks are stored kernel-side, out of the agent's reach — it can't water down its
own exam.

## Working with it

- **Templates** pre-fill proven prompts — themes, new tabs, new tools. Great for a first run.
- You can **steer mid-run** (send a correction while it works) or **cancel**.
- The live log is recoverable — if you navigate away, you can pick the run back up.

> When the agent ships a new feature, it also writes/updates the matching doc in
> **Instructions**, so this manual stays in sync with the app.
