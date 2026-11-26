import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { CodeView } from "../components/CodeView";
import { GraphView, Legend, TYPE_COLORS } from "../components/GraphView";
import { Async, Badge, ErrorNotice, kindTone, shortId } from "../components/ui";
import { useAsync, useDebounced } from "../hooks/useAsync";
import { useRepo } from "./RepoLayout";

const TYPES = ["function", "method", "class", "api_endpoint", "html_element", "css_rule"];
const REL = ["CALLS", "IMPORTS", "HANDLES", "REQUESTS", "TRIGGERS", "REFERENCES", "STYLES", "INHERITS", "USES", "DATA_FLOW", "RETURNS", "READS_STATE"];
const KINDS = ["detected", "inferred", "heuristic"];

function Toggle({ options, value, onChange }: { options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="chips">
      {options.map((o) => {
        const on = value.includes(o);
        return <button key={o} type="button" aria-pressed={on} className={`chip ${on ? "on" : ""}`} onClick={() => onChange(on ? value.filter((x) => x !== o) : [...value, o])}>{o}</button>;
      })}
    </div>
  );
}

export function GraphPage() {
  const { id, repo } = useRepo();
  const langs = Object.keys(repo.language_counts);
  const [level, setLevel] = useState<"entity" | "file">(repo.entity_count > 1500 ? "file" : "entity");
  const [types, setTypes] = useState<string[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [rels, setRels] = useState<string[]>([]);
  const [kinds, setKinds] = useState<string[]>([]);
  const [minConf, setMinConf] = useState(0);
  const [maxNodes, setMaxNodes] = useState(150);
  const [search, setSearch] = useState("");
  const [focus, setFocus] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [edge, setEdge] = useState<{ source: string; type: string; target: string } | null>(null);
  const q = useDebounced(search, 300);

  const graph = useAsync(() => focus
    ? api.neighborhood(id, focus, { depth: 2, max_nodes: maxNodes, rel_types: rels.join(",") })
    : api.graph(id, { level, types: types.join(","), languages: languages.join(","), rel_types: rels.join(","), kinds: kinds.join(","),
        min_confidence: minConf, max_nodes: maxNodes, q, include_files: level === "file" }),
    [id, level, types.join(), languages.join(), rels.join(), kinds.join(), minConf, maxNodes, q, focus]);
  const detail = useAsync(() => api.entity(id, selected!), [id, selected], !!selected && level === "entity");
  const evidence = useAsync(() => api.edge(id, edge!.source, edge!.type, edge!.target), [id, edge?.source, edge?.type, edge?.target], !!edge && level === "entity");
  const presentTypes = useMemo(() => [...new Set((graph.data?.nodes ?? []).map((n) => n.type))], [graph.data]);

  return (
    <div className="stack">
      <div className="card stack">
        <div className="row">
          <label className="field">View
            <select value={level} onChange={(e) => { setLevel(e.target.value as "entity" | "file"); setFocus(null); setSelected(null); }}>
              <option value="entity">Entities (functions, classes…)</option><option value="file">Files (module dependencies)</option>
            </select>
          </label>
          <label className="field grow">Search by name<input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="e.g. login" /></label>
          <label className="field">Max nodes<input type="number" min={5} max={2000} value={maxNodes} onChange={(e) => setMaxNodes(Math.max(5, Number(e.target.value) || 150))} style={{ width: 90 }} /></label>
          <label className="field">Min confidence {minConf.toFixed(1)}<input type="range" min={0} max={1} step={0.1} value={minConf} onChange={(e) => setMinConf(Number(e.target.value))} /></label>
        </div>
        {level === "entity" && <div className="row"><span className="muted small">Types</span><Toggle options={TYPES} value={types} onChange={setTypes} /></div>}
        <div className="row"><span className="muted small">Languages</span><Toggle options={langs} value={languages} onChange={setLanguages} /></div>
        <div className="row"><span className="muted small">Relations</span><Toggle options={REL} value={rels} onChange={setRels} /></div>
        {level === "entity" && <div className="row"><span className="muted small">Link certainty</span><Toggle options={KINDS} value={kinds} onChange={setKinds} /></div>}
        {focus && <div className="notice info row">Showing the neighbourhood of <b className="mono">{shortId(focus)}</b>
          <button className="btn secondary small" onClick={() => setFocus(null)}>Back to full graph</button></div>}
      </div>

      <div className="graph-wrap">
        <div className="stack" style={{ gap: 8 }}>
          {graph.error && <ErrorNotice message={graph.error} />}
          {graph.loading && !graph.data && <div className="empty">Loading graph…</div>}
          {graph.data && (
            <>
              {graph.data.meta?.truncated && <div className="notice">Showing the {graph.data.nodes.length} best-connected of {graph.data.meta.total_nodes} nodes. Narrow the filters or raise “Max nodes”.</div>}
              {graph.data.nodes.length === 0
                ? <div className="card empty">Nothing matches these filters.</div>
                : <GraphView data={graph.data} selected={selected} onNode={(n) => { setSelected(n); setEdge(null); }} onEdge={setEdge} />}
              <div className="row" style={{ justifyContent: "space-between" }}>
                <Legend types={presentTypes.filter((t) => t in TYPE_COLORS)} />
                <span className="muted small">{graph.data.nodes.length} nodes · {graph.data.edges.length} edges</span>
              </div>
            </>
          )}
        </div>

        <aside className="card stack" aria-label="Selection details" style={{ alignSelf: "start", maxHeight: 700, overflow: "auto" }}>
          {!selected && !edge && <div className="muted">Click a node to see what it is, or an edge to see the code that created it.</div>}
          {selected && level === "file" && <div><h3 className="mono">{selected}</h3><button className="btn small" onClick={() => { setLevel("entity"); setSearch(`${selected}:`); }}>Show its entities</button></div>}
          {selected && level === "entity" && (
            <Async state={detail}>
              {(d) => (
                <div className="stack" style={{ gap: 8 }}>
                  <h3 style={{ margin: 0 }}>{d.entity.qualified_name ?? d.entity.name}</h3>
                  <div className="row"><Badge>{d.entity.type}</Badge><Badge>{d.entity.language}</Badge></div>
                  <div className="small muted mono">{d.entity.location ? `${d.entity.location.file_path}:${d.entity.location.start_line}` : ""}</div>
                  <div className="small">{d.outgoing.length} outgoing · {d.incoming.length} incoming links</div>
                  <div className="row">
                    <button className="btn small" onClick={() => setFocus(d.entity.id)}>Focus neighbourhood</button>
                    <Link className="btn secondary small" to={`../impact?target=${encodeURIComponent(d.entity.id)}`}>Impact</Link>
                  </div>
                </div>
              )}
            </Async>
          )}
          {edge && level === "entity" && (
            <div className="stack" style={{ gap: 8 }}>
              <h3 style={{ margin: 0 }}>{edge.type}</h3>
              <div className="small mono">{shortId(edge.source)} → {shortId(edge.target)}</div>
              <Async state={evidence}>
                {(ev) => (
                  <>
                    {graph.data?.edges.filter((e) => e.source === edge.source && e.target === edge.target && e.type === edge.type).map((e) =>
                      <div key="k"><Badge tone={kindTone(e.kind)}>{e.kind}</Badge> <span className="small muted">confidence {e.confidence}</span></div>)}
                    {(ev.evidence ?? []).length === 0 && <div className="muted small">No code evidence recorded for this link.</div>}
                    {(ev.evidence ?? []).slice(0, 3).map((x, i) => (
                      <div key={i}>
                        {x.location && <div className="small mono">{x.location.file_path}:{x.location.start_line}</div>}
                        {x.snippet && <div className="snippet">{x.snippet}</div>}
                        {x.location && !x.snippet && <CodeView repoId={id} file={x.location.file_path} start={Math.max(1, x.location.start_line - 2)} end={x.location.end_line + 2} highlight={[x.location.start_line, x.location.end_line]} />}
                      </div>
                    ))}
                  </>
                )}
              </Async>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
