import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { EntityRow } from "../api/types";
import { CodeView } from "../components/CodeView";
import { Async, Badge, kindTone, shortId } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { useRepo } from "./RepoLayout";

interface Dir { name: string; dirs: Map<string, Dir>; files: EntityRow[] }

export function buildTree(files: EntityRow[]): Dir {
  const root: Dir = { name: "", dirs: new Map(), files: [] };
  for (const f of files) {
    const parts = f.name.split("/");
    let cur = root;
    for (const p of parts.slice(0, -1)) {
      if (!cur.dirs.has(p)) cur.dirs.set(p, { name: p, dirs: new Map(), files: [] });
      cur = cur.dirs.get(p)!;
    }
    cur.files.push(f);
  }
  return root;
}

function DirView({ dir, depth, selected, onSelect }: { dir: Dir; depth: number; selected: string | null; onSelect: (f: EntityRow) => void }) {
  const [open, setOpen] = useState(depth < 1);
  const pad = { paddingLeft: depth * 12 + 6 };
  return (
    <div>
      {dir.name && <button className="node" style={pad} onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "▾" : "▸"} {dir.name}</button>}
      {(open || !dir.name) && (
        <>
          {[...dir.dirs.values()].sort((a, b) => a.name.localeCompare(b.name)).map((d) => (
            <DirView key={d.name} dir={d} depth={depth + 1} selected={selected} onSelect={onSelect} />
          ))}
          {dir.files.sort((a, b) => a.name.localeCompare(b.name)).map((f) => (
            <button key={f.id} className={`node ${selected === f.id ? "sel" : ""}`} style={{ paddingLeft: (depth + 1) * 12 + 6 }} onClick={() => onSelect(f)}>
              {f.name.split("/").pop()}
            </button>
          ))}
        </>
      )}
    </div>
  );
}

export function ExplorerPage() {
  const { id } = useRepo();
  const files = useAsync(() => api.entities(id, { type: "file", limit: 1000 }), [id]);
  const [file, setFile] = useState<EntityRow | null>(null);
  const [entity, setEntity] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const shown = useMemo(() => (files.data ?? []).filter((f) => f.name.toLowerCase().includes(filter.toLowerCase())), [files.data, filter]);
  const tree = useMemo(() => buildTree(shown), [shown]);

  const inFile = useAsync(() => api.graph(id, { q: `${file!.name}:`, include_files: false }), [id, file?.name], !!file);
  const detail = useAsync(() => api.entity(id, entity!), [id, entity], !!entity);
  const loc = detail.data?.entity.location;

  return (
    <div className="split">
      <div className="card">
        <input type="search" placeholder="Filter files…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "100%", marginBottom: 8 }} aria-label="Filter files" />
        <div className="tree">
          <Async state={files}>{() => shown.length ? <DirView dir={tree} depth={0} selected={file?.id ?? null} onSelect={(f) => { setFile(f); setEntity(null); }} /> : <div className="muted">No files match.</div>}</Async>
        </div>
      </div>

      <div className="stack">
        {!file && <div className="card empty">Select a file to see its source and the entities defined in it.</div>}
        {file && (
          <>
            <div className="card stack">
              <h2 className="mono" style={{ margin: 0 }}>{file.name}</h2>
              <Async state={inFile}>
                {(g) => (
                  <div className="chips" aria-label="Entities in this file">
                    {g.nodes.length === 0 && <span className="muted">No named entities.</span>}
                    {g.nodes.filter((n) => n.id !== file.id).sort((a, b) => a.label.localeCompare(b.label)).map((n) => (
                      <button key={n.id} className={`chip ${entity === n.id ? "on" : ""}`} onClick={() => setEntity(n.id)} title={n.id}>{n.type} · {shortId(n.id)}</button>
                    ))}
                  </div>
                )}
              </Async>
              <CodeView repoId={id} file={file.name} highlight={loc && loc.file_path === file.name ? [loc.start_line, loc.end_line] : undefined} />
            </div>

            {entity && (
              <div className="card stack">
                <Async state={detail}>
                  {(d) => (
                    <>
                      <div className="row" style={{ justifyContent: "space-between" }}>
                        <h2 style={{ margin: 0 }}>{d.entity.qualified_name ?? d.entity.name} <Badge>{d.entity.type}</Badge></h2>
                        <Link className="btn secondary small" to={`../impact?target=${encodeURIComponent(d.entity.id)}`}>Impact of changing this</Link>
                      </div>
                      {d.entity.docstring && <p className="muted" style={{ margin: 0 }}>{d.entity.docstring}</p>}
                      <div className="grid cols-2">
                        {([["Uses / calls (outgoing)", d.outgoing], ["Used by (incoming)", d.incoming]] as const).map(([title, edges]) => (
                          <div key={title}>
                            <h3>{title} · {edges.length}</h3>
                            <table className="t"><tbody>
                              {edges.slice(0, 40).map((e, i) => {
                                const other = title.startsWith("Uses") ? e.target : e.source;
                                return (
                                  <tr key={i}>
                                    <td><Badge>{e.type}</Badge></td>
                                    <td><button className="chip" onClick={() => setEntity(other)} title={other}>{shortId(other)}</button></td>
                                    <td><Badge tone={kindTone(e.kind)}>{e.kind}</Badge></td>
                                  </tr>
                                );
                              })}
                            </tbody></table>
                            {edges.length > 40 && <div className="small muted">…and {edges.length - 40} more</div>}
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </Async>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
