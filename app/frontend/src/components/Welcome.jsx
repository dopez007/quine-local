import React from "react";
import { Modal, Button } from "./index.jsx";
import Logo from "../Logo.jsx";

// First-run orientation. App gates this to show once per browser (localStorage "quine-welcomed")
// and lets you reopen it any time from the header "?" or the command palette. Written for the
// primary audience — people with ideas, not engineering backgrounds: the two core moves (chat
// vs. asking the app to change itself) plus the fail-safe promise, each with a button that
// jumps straight to that tab.
const STEPS = [
  {
    key: "run",
    tab: "run",
    title: "Chat with it",
    body: "Ask anything. It searches the web, reads your uploaded documents, and builds things " +
      "for you in a safe sandbox — answers stream live.",
    cta: "Start chatting",
    icon: (
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    ),
  },
  {
    key: "modify",
    tab: "modify",
    title: "Ask it to change itself",
    body: "Describe a feature the way you'd say it — “add a night mode” — and Quine rewrites " +
      "its own code, checks its work, and ships the new version. That's the headline trick.",
    cta: "Open Self-Modify",
    icon: (
      <>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </>
    ),
  },
  {
    key: "safe",
    tab: "versions",
    title: "You can't lose anything",
    body: "Every change is a saved version you can undo in one click — and your documents, " +
      "conversations, and knowledge live outside the app, untouched by any change.",
    cta: "See Versions",
    icon: (
      <>
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        <path d="M9 12l2 2 4-4" />
      </>
    ),
  },
];

export default function Welcome({ onClose, onNavigate }) {
  const go = (tab) => {
    onNavigate?.(tab);
    onClose?.();
  };
  const title = (
    <span className="welcome-title">
      <span className="welcome-mark"><Logo size={20} /></span> Welcome to Quine
    </span>
  );
  return (
    <Modal title={title} onClose={onClose}>
      <p className="welcome-lead">
        This is your <strong>fail-safe workspace</strong> — an app that becomes what you ask for.
        Three things to know:
      </p>
      <div className="welcome-grid">
        {STEPS.map((s) => (
          <div className="welcome-step" key={s.key}>
            <span className="welcome-icon" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                {s.icon}
              </svg>
            </span>
            <h3>{s.title}</h3>
            <p>{s.body}</p>
            <Button variant="ghost" onClick={() => go(s.tab)}>{s.cta} →</Button>
          </div>
        ))}
      </div>
      <div className="welcome-foot">
        <span className="muted">Reopen this any time from <strong>?</strong> in the header or the command palette (⌘/Ctrl&nbsp;K).</span>
        <Button variant="primary" onClick={onClose}>Start exploring</Button>
      </div>
    </Modal>
  );
}
