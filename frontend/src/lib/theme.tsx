// Theme (light / dark). Persisted in localStorage under `vw:theme`; on first
// visit we seed from `prefers-color-scheme`. Applying a theme writes
// `data-theme="light"|"dark"` on <html>, which the tokens in index.css key off.

import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "vw:theme";

function readStored(): Theme | null {
  const v = localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" ? v : null;
}

function systemTheme(): Theme {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function initialTheme(): Theme {
  return readStored() ?? systemTheme();
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  root.style.colorScheme = theme;
}

interface ThemeCtx {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => initialTheme());

  useEffect(() => { applyTheme(theme); }, [theme]);

  // Follow the OS if the user hasn't picked one explicitly.
  useEffect(() => {
    if (readStored()) return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => { if (!readStored()) setThemeState(mq.matches ? "dark" : "light"); };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const setTheme = useCallback((t: Theme) => {
    localStorage.setItem(STORAGE_KEY, t);
    setThemeState(t);
  }, []);
  const toggle = useCallback(() => setTheme(theme === "dark" ? "light" : "dark"), [theme, setTheme]);

  return <Ctx.Provider value={{ theme, setTheme, toggle }}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTheme must be used within <ThemeProvider>");
  return v;
}
