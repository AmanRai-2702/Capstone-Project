import { FormEvent, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { ImpactReport, ImpactedComponent } from "../api/types";
import { EntityPicker } from "../components/EntityPicker";
import { Badge, ErrorNotice, Spinner, Stat, Bars, kindTone, shortId } from "../components/ui";
import { useRepo } from "./RepoLayout";

const CATEGORY_HELP: Record<string, string> = {
  direct: "depends on the changed code itself", execution_flow: "reached by calls / requests / handlers",
  data_flow: "reached through data passed or returned", indirect: "reached through imports, references or styling",
};

function Component({ c }: { c: ImpactedComponent }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        <td><button className="chip" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "▾" : "▸"} {shortId(c.entity_id)}</button></td>
        <td><Badge>{c.type}</Badge></td><td>{c.distance}</td>
        <td><Badge tone={c.uncertain ? "warn" : "ok"}>{c.category}</Badge></td>
        <td>{c.confidence.toFixed(2)}</td><td className="mono small">{c.location}</td>
      </tr>
      {open && (
        <tr><td colSpan={6}>
          <div className="small" style={{ marginBottom: 6 }}>{c.explanation}</div>
          <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
            {c.chain.map((h, i) => (
              <li key={i}><span className="mono">{shortId(h.dependent)}</span> —<Badge>{h.relation}</Badge><Badge tone={kindTone(h.kind)}>{h.kind}</Badge>→ <span className="mono">{shortId(h.dependency)}</span>
                {h.evidence && <div className="snippet" style={{ margin: "3px 0" }}>{h.evidence}</div>}</li>
            ))}
          </ol>
        </td></tr>
      )}
    </>
  );
}

export function ImpactPage() {
  const { id } = useRepo();
  const [params] = useSearchParams();
  const [mode, setMode] = useState<"target" | "diff">("target");
  const [target, setTarget] = useState(params.get("target") ?? "");
  const [diff, setDiff] = useState("");
  const [strict, setStrict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ImpactReport | null>(null);

  async function run(e?: FormEvent, override?: string) {
    e?.preventDefault();
    setBusy(true); setError(null); setReport(null);
    try { setReport(await api.impact(id, mode === "diff" ? { diff, strict_references: strict } : { target: (override ?? target).trim(), strict_references: strict })); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }
  // arriving from the Explorer / Graph with ?target= runs the analysis straight away
  useEffect(() => { const t = params.get("target"); if (t) { setTarget(t); void run(undefined, t); } /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [params, id]);

  const all = report ? [...report.direct, ...report.transitive] : [];
  return (
    <div className="stack">
      <form className="card stack" onSubmit={(e) => run(e)} aria-label="Impact analysis">
        <div><h2>What breaks if I change this?</h2><div className="muted">Pick a function, class, endpoint or element, or paste a git diff.</div></div>
        <div className="chips"><button type="button" className={`chip ${mode === "target" ? "on" : ""}`} onClick={() => setMode("target")}>A component</button>
          <button type="button" className={`chip ${mode === "diff" ? "on" : ""}`} onClick={() => setMode("diff")}>A git diff</button></div>
        {mode === "target"
          ? <EntityPicker repoId={id} label="Component to change" value={target} onChange={setTarget} placeholder="e.g. get_user" />
          : <label className="field">Unified diff (output of <code>git diff</code>)<textarea value={diff} onChange={(e) => setDiff(e.target.value)} placeholder={"diff --git a/app.py b/app.py\n@@ -3,2 +3,2 @@ …"} /></label>}
        <label className="row small"><input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} /> Strict mode: do not follow element references (fewer, more precise “indirect” results)</label>
        <div className="row"><button className="btn" disabled={busy || (mode === "target" ? !target.trim() : !diff.trim())}>Analyze impact</button>{busy && <Spinner />}</div>
      </form>
      {error && <ErrorNotice message={error} />}
      {report && (
        <>
          <div className="grid cols-4">
            <div className="card stat"><div className="value"><Badge tone={report.risk}>{report.risk.toUpperCase()}</Badge></div><div className="label">Risk</div><div className="small muted">{report.risk_reason}</div></div>
            <Stat label="Impacted components" value={report.blast_radius} hint={`${report.direct.length} direct, ${report.transitive.length} transitive`} />
            <Stat label="Entry points affected" value={report.affected_entry_points.length} hint="buttons / endpoints" />
            <Stat label="Tests to re-run" value={report.suggested_test_details.length} />
          </div>
          {report.notes.map((n) => <div key={n} className="notice info">{n}</div>)}
          <div className="grid cols-2">
            <div className="card"><h2>Why this risk level</h2>
              {report.risk_factors.length === 0 ? <div className="muted">No risk factors found.</div> : report.risk_factors.map((f) => (
                <div key={f.factor} style={{ marginBottom: 8 }}><Badge tone={f.severity === "high" ? "high" : f.severity === "medium" ? "medium" : ""}>{f.severity}</Badge> <b>{f.factor.replace(/_/g, " ")}</b>
                  <div className="small">{f.detail}</div>{f.evidence.length > 0 && <div className="small mono muted">{f.evidence.slice(0, 4).join(" · ")}</div>}</div>))}
            </div>
            <div className="card"><h2>By category</h2><Bars data={report.by_category} />
              <div className="small muted" style={{ marginTop: 8 }}>{Object.entries(CATEGORY_HELP).map(([k, v]) => <div key={k}><b>{k}</b>: {v}</div>)}</div></div>
          </div>
          {report.suggested_test_details.length > 0 && (
            <div className="card"><h2>Suggested tests</h2>
              <table className="t"><thead><tr><th>Test file</th><th>Score</th><th>Why</th></tr></thead><tbody>
                {report.suggested_test_details.map((t) => <tr key={t.file}><td className="mono">{t.file}</td><td>{t.score}</td><td className="small">{t.reasons.join("; ")}</td></tr>)}
              </tbody></table></div>)}
          <div className="card"><h2>Impacted components ({all.length})</h2>
            {all.length === 0 ? <div className="muted">Nothing depends on this component.</div> : (
              <table className="t"><thead><tr><th>Component</th><th>Type</th><th>Hops</th><th>Category</th><th>Confidence</th><th>Location</th></tr></thead>
                <tbody>{all.map((c) => <Component key={c.entity_id} c={c} />)}</tbody></table>)}
          </div>
        </>
      )}
    </div>
  );
}
