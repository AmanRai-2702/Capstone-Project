import { useEffect, useState } from "react";
import { NavLink, Outlet, useMatch } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { Badge } from "./ui";

export const REPO_TABS = [
  ["", "Overview"], ["explorer", "Explorer"], ["graph", "Graph"], ["trace", "Trace"],
  ["impact", "Impact"], ["contracts", "API contracts"], ["chat", "Ask"],
] as const;

function LlmStatusChip() {
  const status = useAsync(() => api.llmStatus(), []);
  if (status.error) return <Badge tone="bad">API offline</Badge>;
  const s = status.data;
  if (!s) return null;
  return (
    <div className="small" aria-label="LLM status">
      <div className="muted">Answer engine</div>
      <div className="row" style={{ gap: 6 }}>
        <Badge tone={s.provider === "template" ? "" : "ok"}>{s.provider}</Badge>
        {s.model && <span className="mono" title={s.model}>{s.model.split("/").pop()}</span>}
      </div>
      {s.fallback && <div className="notice small" style={{ marginTop: 6 }}>Requested “{s.requested_provider}” unavailable ({s.fallback_reason}); using offline answers.</div>}
      {!s.external_llm_allowed && <div className="muted" style={{ marginTop: 4 }}>External LLM disabled</div>}
    </div>
  );
}

function useTheme() {
  const [theme, setTheme] = useState<string>(() => { try { return localStorage.getItem("theme") ?? "auto"; } catch { return "auto"; } });
  useEffect(() => {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch { /* private mode */ }
  }, [theme]);
  return [theme, setTheme] as const;
}

export function Layout() {
  const inRepo = useMatch("/repo/:id/*");
  const id = inRepo?.params.id;
  const [theme, setTheme] = useTheme();
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="logo">R</span> RepoMind</div>
        <nav className="nav" aria-label="Main">
          <NavLink to="/" end>Repositories</NavLink>
          {id && (
            <>
              <div className="nav-label">Current repository</div>
              {REPO_TABS.map(([path, label]) => (
                <NavLink key={path} to={`/repo/${encodeURIComponent(id)}${path ? `/${path}` : ""}`} end={path === ""}>{label}</NavLink>
              ))}
            </>
          )}
        </nav>
        <div className="spacer" />
        <LlmStatusChip />
        <label className="field" style={{ marginTop: 10 }}>
          Theme
          <select value={theme} onChange={(e) => setTheme(e.target.value)}>
            <option value="auto">System</option><option value="light">Light</option><option value="dark">Dark</option>
          </select>
        </label>
      </aside>
      <main className="main"><Outlet /></main>
    </div>
  );
}
