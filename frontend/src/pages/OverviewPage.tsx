import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { Async, Bars, Badge, Stat } from "../components/ui";
import { useRepo } from "./RepoLayout";

export function OverviewPage() {
  const { id, repo } = useRepo();
  const overview = useAsync(() => api.overview(id), [id]);
  const unresolved = Object.values(repo.unresolved_counts_by_reason).reduce((a, b) => a + b, 0);
  return (
    <div className="stack">
      <div className="grid cols-4">
        <Stat label="Source files" value={repo.source_file_count} hint={`${repo.file_count} files in total`} />
        <Stat label="Entities" value={repo.entity_count.toLocaleString()} />
        <Stat label="Relationships" value={repo.relationship_count.toLocaleString()} />
        <Stat label="Unresolved links" value={unresolved.toLocaleString()} hint="calls RepoMind could not connect" />
      </div>

      {repo.analysis_errors.length > 0 && (
        <div className="notice" role="alert">
          <b>{repo.analysis_errors.length} file(s) could not be fully analysed:</b>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{repo.analysis_errors.slice(0, 5).map((e) => <li key={e} className="mono small">{e}</li>)}</ul>
        </div>
      )}

      <div className="grid cols-2">
        <div className="card"><h2>Languages</h2><Bars data={repo.language_counts} /></div>
        <div className="card"><h2>Entities by type</h2><Bars data={repo.entity_counts_by_type} /></div>
        <div className="card">
          <h2>Relationships by type</h2><Bars data={repo.relationship_counts_by_type} />
        </div>
        <div className="card">
          <h2>How sure is each link?</h2>
          <Bars data={repo.relationship_counts_by_kind} />
          <p className="small muted" style={{ marginBottom: 0 }}>
            <Badge tone="detected">detected</Badge> read directly from code · <Badge tone="inferred">inferred</Badge> resolved by rules · <Badge tone="heuristic">heuristic</Badge> a labelled guess.
          </p>
        </div>
      </div>

      <div className="card">
        <h2>What this repository is</h2>
        <Async state={overview}>
          {(o) => (
            <>
              {o.headline && <p><b>{o.headline}</b></p>}
              <ol style={{ paddingLeft: 20, margin: 0 }}>
                {o.facts.map((f, i) => (
                  <li key={i} style={{ marginBottom: 8 }}>
                    {f.text} {f.citation && <span className="cite">{f.citation}</span>}
                    {f.snippet && <div className="snippet" style={{ marginTop: 4 }}>{f.snippet}</div>}
                  </li>
                ))}
              </ol>
            </>
          )}
        </Async>
      </div>
    </div>
  );
}
