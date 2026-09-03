import React from "react";
import { useTheme } from "../useTheme.js";

// Appearance control for the harness header: light/dark only. Renders as a one-click sun/moon
// toggle in the header (default) or as a labeled Light/Dark segmented control inline (Settings,
// `inline` prop). Self-contained — useTheme owns the data-theme attribute on <html> that theme.css
// keys off of. The accent is a single fixed brand purple, so there is no picker.

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}

export default function Appearance({ inline = false }) {
  const { theme, toggle } = useTheme();

  if (inline) {
    return (
      <div className="appearance-inline">
        <div className="ap-row">
          <span className="ap-label">Theme</span>
          <div className="seg" role="group" aria-label="Theme">
            <button className={"seg-btn" + (theme === "light" ? " on" : "")} onClick={() => theme !== "light" && toggle()}>
              Light
            </button>
            <button className={"seg-btn" + (theme === "dark" ? " on" : "")} onClick={() => theme !== "dark" && toggle()}>
              Dark
            </button>
          </div>
        </div>
      </div>
    );
  }

  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      className="btn ghost icon theme-toggle"
      onClick={toggle}
      title={`Switch to ${next} theme`}
      aria-label={`Switch to ${next} theme`}
    >
      {theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}
