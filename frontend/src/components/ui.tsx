import type { ReactNode } from "react";
import type { AsyncState } from "../hooks/useAsync";

export const Spinner = () => <span className="spinner" role="status" aria-label="Loading" />;

export function ErrorNotice({ message }: { message: string }) {
  return <div className="notice bad" role="alert">{message}</div>;
}

export function Badge({ tone, children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge ${tone ?? ""}`}>{children}</span>;
}

/** Renders loading / error / data for an async state, so each page handles all three the same way. */
export function Async<T>({ state, children }: { state: AsyncState<T>; children: (data: T) => ReactNode }) {
  if (state.error) return <ErrorNotice message={state.error} />;
  if (state.loading && !state.data) return <div className="empty"><Spinner /></div>;
  if (!state.data) return null;
  return <>{children(state.data)}</>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="card stat">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
      {hint && <div className="small muted">{hint}</div>}
    </div>
  );
}

export function Bars({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, n]) => n));
  if (!entries.length) return <div className="muted">None</div>;
  return (
    <div className="stack" style={{ gap: 8 }}>
      {entries.map(([k, n]) => (
        <div key={k}>
          <div className="row" style={{ justifyContent: "space-between" }}><span>{k}</span><b>{n.toLocaleString()}</b></div>
          <div className="bar"><span style={{ width: `${(n / max) * 100}%` }} /></div>
        </div>
      ))}
    </div>
  );
}

export const kindTone = (kind: string | null | undefined) => (kind === "detected" ? "detected" : kind === "inferred" ? "inferred" : kind === "heuristic" ? "heuristic" : "");

/** "path/file.py:Class.method" -> "Class.method" for compact display (the full id stays in the title). */
export const shortId = (id: string) => {
  const parts = id.split(":");
  return parts.length >= 3 ? parts.slice(2).join(":") : parts[parts.length - 1];
};
export const fileOf = (location: string | null | undefined) => (location ? location.split(":")[0] : null);
export const parseCitation = (c: string): { file: string; start: number; end: number } | null => {
  const m = /^(.+?):(\d+)(?:-(\d+))?$/.exec(c);
  return m ? { file: m[1], start: Number(m[2]), end: Number(m[3] ?? m[2]) } : null;
};
