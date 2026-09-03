import { useEffect, useState } from "react";

const KEY = "quine-theme";
const OLD_KEY = "aimprove-theme"; // pre-rebrand key — migrated once

// Dark/light theme persisted in localStorage; applied via data-theme on <html>, which the
// CSS variables in theme.css key off of. First visit follows the OS preference; the manual
// choice is then persisted. The accent is a single fixed brand purple (see theme.css), so
// there is no accent state here anymore.
function initial() {
  let saved = localStorage.getItem(KEY);
  if (!saved) {
    const old = localStorage.getItem(OLD_KEY);
    if (old) {
      saved = old;
      localStorage.setItem(KEY, old);
    }
    localStorage.removeItem(OLD_KEY);
  }
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function useTheme() {
  const [theme, setTheme] = useState(initial);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(KEY, theme);
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}
