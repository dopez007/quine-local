import React, { useEffect, useState } from "react";
import { useTheme } from "./useTheme.js";
import { sysGet, appGet, apiUrl } from "./api.js";
import { Badge, Button } from "./components";
import Logo from "./Logo.jsx";
import CommandPalette from "./components/CommandPalette.jsx";
import Appearance from "./components/Appearance.jsx";
import Welcome from "./components/Welcome.jsx";
import ReconnectOverlay from "./components/ReconnectOverlay.jsx";
import RunTab from "./tabs/RunTab.jsx";
import SelfModifyTab from "./tabs/SelfModifyTab.jsx";
import VersionsTab from "./tabs/VersionsTab.jsx";
import AuditTab from "./tabs/AuditTab.jsx";
import SettingsTab from "./tabs/SettingsTab.jsx";
import UsageTab from "./tabs/UsageTab.jsx";
import PluginsTab from "./tabs/PluginsTab.jsx";
import KnowledgeTab from "./tabs/KnowledgeTab.jsx";
import DevelopmentTab from "./tabs/DevelopmentTab.jsx";
import InstructionsTab from "./tabs/InstructionsTab.jsx";
import ErrorsTab from "./tabs/ErrorsTab.jsx";
import KernelTab from "./tabs/KernelTab.jsx";

// SVG icons for each tab — clean, professional, theme-agnostic
const TabIcon = ({ name }) => {
  const icons = {
    run: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      </svg>
    ),
    modify: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </svg>
    ),
    versions: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" />
      </svg>
    ),
    usage: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="12" y1="20" x2="12" y2="10" /><line x1="18" y1="20" x2="18" y2="4" /><line x1="6" y1="20" x2="6" y2="16" />
      </svg>
    ),
    audit: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" /><polyline points="10 9 9 9 8 9" />
      </svg>
    ),
    settings: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    ),
    plugins: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10 2v4M14 2v4M5 10h14a1 1 0 0 1 1 1v2a5 5 0 0 1-5 5h-1v3H9v-3H8a5 5 0 0 1-5-5v-2a1 1 0 0 1 1-1z" />
      </svg>
    ),
    knowledge: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
      </svg>
    ),
    development: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
      </svg>
    ),
    instructions: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
      </svg>
    ),
    errors: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    ),
    kernel: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="6" y="6" width="12" height="12" rx="1" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" />
      </svg>
    ),
  };
  return <span className="sidebar-icon">{icons[name] || null}</span>;
};

const TABS = [
  { id: "run", label: "Run", Comp: RunTab },
  { id: "knowledge", label: "Knowledge", Comp: KnowledgeTab },
  { id: "modify", label: "Self-Modify", Comp: SelfModifyTab },
  { id: "development", label: "Development", Comp: DevelopmentTab },
  { id: "versions", label: "Versions", Comp: VersionsTab },
  { id: "plugins", label: "Plugins", Comp: PluginsTab },
  { id: "usage", label: "Usage", Comp: UsageTab },
  { id: "errors", label: "Errors", Comp: ErrorsTab },
  { id: "audit", label: "Audit", Comp: AuditTab },
  { id: "kernel", label: "Kernel", Comp: KernelTab },
  { id: "settings", label: "Settings", Comp: SettingsTab },
  { id: "instructions", label: "Instructions", Comp: InstructionsTab },
];

export default function App() {
  // Appearance owns the on-screen controls, but the command palette still exposes a theme toggle,
  // so capture `theme` + `toggle` here (referencing them without binding throws ReferenceError and
  // blanks the whole app during render).
  const { theme, toggle } = useTheme();
  const [tab, setTab] = useState("run");
  const [status, setStatus] = useState(null);
  const [build, setBuild] = useState("");
  // Which preview env this browser is looking at (from /health, which is cookie-routed
  // through the gateway — so it names the env that actually answered). null = production.
  const [preview, setPreview] = useState(null);
  // Open by default on desktop; collapsed on mobile so the drawer doesn't cover
  // content on first load (it's an off-canvas overlay at ≤720px — see theme.css).
  const [sidebarOpen, setSidebarOpen] = useState(
    () => typeof window === "undefined" || window.innerWidth > 720
  );
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [showWelcome, setShowWelcome] = useState(false);
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  // First-run orientation: show the welcome once per browser; reopen on demand keeps the flag.
  const closeWelcome = () => {
    localStorage.setItem("quine-welcomed", "1");
    setShowWelcome(false);
  };

  // Navigate to a tab, and dismiss the drawer on mobile (where it overlays content).
  const selectTab = (id) => {
    setTab(id);
    if (typeof window !== "undefined" && window.innerWidth <= 720) setSidebarOpen(false);
  };

  async function refresh() {
    try {
      setStatus(await sysGet("/status"));
    } catch {
      /* kernel unreachable */
    }
    try {
      setBuild((await appGet("/api/app/info")).build);
    } catch {
      /* ignore */
    }
    try {
      setPreview((await appGet("/health")).preview || null);
    } catch {
      /* ignore */
    }
  }
  useEffect(() => {
    refresh();
    if (!localStorage.getItem("quine-welcomed")) setShowWelcome(true);
  }, []);

  // Cmd/Ctrl+K toggles the command palette.
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const commands = [
    ...TABS.map((t) => ({ id: "tab-" + t.id, label: t.label, hint: "Tab", action: () => selectTab(t.id) })),
    {
      id: "toggle-theme",
      label: theme === "dark" ? "Switch to light theme" : "Switch to dark theme",
      hint: "Theme",
      action: toggle,
    },
    {
      id: "toggle-sidebar",
      label: sidebarOpen ? "Collapse sidebar" : "Expand sidebar",
      hint: "Layout",
      action: () => setSidebarOpen((o) => !o),
    },
    {
      id: "getting-started",
      label: "Getting started",
      hint: "Help",
      action: () => setShowWelcome(true),
    },
  ];

  const Active = TABS.find((t) => t.id === tab).Comp;
  return (
    <div className={"app" + (sidebarOpen ? "" : " app-sidebar-closed")}>
      {preview && (
        <div className="preview-banner" role="status">
          <span className="preview-banner-dot" aria-hidden="true" />
          <strong>PREVIEW: {preview}</strong>
          <span className="preview-banner-note">
            you're looking at a preview environment — this is not production
          </span>
          <a className="preview-banner-exit" href={apiUrl("/preview/exit")}>
            Exit preview
          </a>
        </div>
      )}
      <header className="header">
        <button
          className="sidebar-toggle header-sidebar-toggle"
          onClick={() => setSidebarOpen((o) => !o)}
          title={sidebarOpen ? "Close sidebar" : "Open sidebar"}
          aria-label="Toggle navigation sidebar"
        >
          <span className="hamburger">
            <span /><span /><span />
          </span>
        </button>
        <div className="brand">
          <span className="logo">
            <Logo size={16} animate />
          </span>
          <span className="title">Quine</span>
        </div>
        <div className="badges">
          {build && <Badge>{build}</Badge>}
          {status?.active && <Badge tone="accent">active {status.active.short}</Badge>}
          <button
            className="kbd-hint"
            onClick={() => setPaletteOpen(true)}
            title="Command palette"
            aria-label="Open command palette"
          >
            {isMac ? "⌘" : "Ctrl"} K
          </button>
          <Button variant="ghost icon" onClick={() => setShowWelcome(true)} title="Getting started" aria-label="Getting started">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
          </Button>
          <Appearance />
        </div>
      </header>

      <div className="app-body">
        <aside className={"sidebar" + (sidebarOpen ? " open" : "")}>
          <nav className="sidebar-nav">
            {TABS.map((t) => (
              <button
                key={t.id}
                className={"sidebar-item" + (t.id === tab ? " active" : "")}
                onClick={() => selectTab(t.id)}
                title={sidebarOpen ? "" : t.label}
                aria-current={t.id === tab ? "page" : undefined}
              >
                <TabIcon name={t.id} />
                <span className="sidebar-label">{t.label}</span>
              </button>
            ))}
          </nav>
          <div className="sidebar-footer">
            <span className="sidebar-mini-brand" title="Quine">
              <Logo size={18} />
            </span>
          </div>
        </aside>

        {/* On mobile the sidebar is an off-canvas drawer; this backdrop dims the
            content behind the open drawer and closes it on tap (no-op on desktop,
            where .sidebar-overlay is display:none). */}
        {sidebarOpen && (
          <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />
        )}

        <main className={"main" + (tab === "run" || tab === "modify" ? " bleed" : "")}>
          <Active onStatus={refresh} status={status} onNavigate={selectTab} />
        </main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />

      {showWelcome && <Welcome onClose={closeWelcome} onNavigate={selectTab} />}

      <ReconnectOverlay />
    </div>
  );
}
