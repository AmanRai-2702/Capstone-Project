import { FormEvent, useState } from "react";
import { api } from "../api/client";
import type { TraceResult } from "../api/types";
import { EntityPicker } from "../components/EntityPicker";
import { Badge, ErrorNotice, Spinner, kindTone, shortId } from "../components/ui";
import { useRepo } from "./RepoLayout";

const VIEWS: [string, string][] = [
  ["execution", "Execution: what runs after what"], ["dependency", "Dependency: what needs what"],
  ["impact", "Impact: what is affected by what"], ["syntactic", "Syntactic: raw relationships"],
];

export function TracePage() {
  const { id } = useRepo();
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [view, setView] = useState("execution");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TraceResult | null>(null);

  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null); setResult(null);
    try { setResult(await api.trace(id, source.trim(), target.trim(), view)); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }

  return (
    <div className="stack">
      <form className="card stack" onSubmit={run} aria-label="Trace a path">
        <div><h2>Trace a path between two components</h2>
          <div className="muted">For example from a button to the database function it eventually reaches. Each hop shows how sure RepoMind is.</div></div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="grow"><EntityPicker repoId={id} label="From" value={source} onChange={setSource} placeholder="e.g. #loginBtn or login" /></div>
          <div className="grow"><EntityPicker repoId={id} label="To" value={target} onChange={setTarget} placeholder="e.g. get_user" /></div>
          <label className="field">View<select value={view} onChange={(e) => setView(e.target.value)}>{VIEWS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
          <button className="btn" disabled={busy || !source.trim() || !target.trim()}>Trace</button>
          {busy && <Spinner />}
        </div>
      </form>
      {error && <ErrorNotice message={error} />}
      {result && (
        <div className="stack">
          {Object.entries(result.ambiguous).map(([name, ids]) => (
            <div key={name} className="notice info">“{name}” matched several entities; using the first. Others: {ids.slice(1).map(shortId).join(", ")}</div>
          ))}
          {result.paths.length === 0
            ? <div className="card empty">No path from <b>{shortId(result.source)}</b> to <b>{shortId(result.target)}</b> in the {result.view} view. They may be unconnected, or the link may be one RepoMind cannot see statically.</div>
            : result.paths.map((p, i) => (
              <div key={i} className="card stack" style={{ gap: 8 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h3 style={{ margin: 0 }}>Path {i + 1} · {p.hops.length} hop(s)</h3>
                  <div className="row"><span className="small muted">confidence {p.confidence}</span>{p.uncertain && <Badge tone="warn">contains inferred links</Badge>}</div>
                </div>
                <ol style={{ margin: 0, paddingLeft: 20 }}>
                  <li className="mono" title={p.nodes[0]}>{shortId(p.nodes[0])}</li>
                  {p.hops.map((h, j) => (
                    <li key={j}>
                      <span className="muted small">↓ </span><Badge>{h.type ?? "?"}</Badge>{h.reversed && <Badge>reversed</Badge>}{h.derived && <Badge>derived</Badge>} <Badge tone={kindTone(h.kind)}>{h.kind}</Badge>
                      <div className="mono" title={h.to}>{shortId(h.to)}</div>
                    </li>
                  ))}
                </ol>
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
