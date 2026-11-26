import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { Async, Badge, ErrorNotice, Spinner } from "../components/ui";

type Mode = "url" | "path" | "zip";

function AnalyzeForm({ onDone }: { onDone: (repoId: string) => void }) {
  const [mode, setMode] = useState<Mode>("url");
  const [location, setLocation] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  async function poll(jobId: string) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1200));
      if (!alive.current) return null;
      const job = await api.job(jobId);
      if (job.status === "done" && job.repo_id) return job.repo_id;
      if (job.status === "error") throw new Error(job.error ?? "Analysis failed.");
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      let repoId: string | null;
      if (mode === "zip") {
        if (!file) throw new Error("Choose a .zip file first.");
        repoId = (await api.upload(file)).repo_id;
      } else {
        if (!location.trim()) throw new Error(mode === "url" ? "Enter a GitHub URL." : "Enter a folder path.");
        repoId = await poll((await api.analyze(location.trim())).job_id);
      }
      if (repoId) onDone(repoId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit} aria-label="Analyze a repository">
      <h2>Analyze a repository</h2>
      <div className="chips" role="tablist">
        {([["url", "GitHub URL"], ["path", "Local folder"], ["zip", "ZIP upload"]] as const).map(([m, label]) => (
          <button type="button" key={m} role="tab" aria-selected={mode === m} className={`chip ${mode === m ? "on" : ""}`} onClick={() => setMode(m)}>{label}</button>
        ))}
      </div>
      {mode === "zip" ? (
        <label className="field">ZIP archive of the repository
          <input type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
      ) : (
        <label className="field">{mode === "url" ? "Public GitHub repository URL" : "Absolute path on the machine running the API"}
          <input type="text" value={location} onChange={(e) => setLocation(e.target.value)}
            placeholder={mode === "url" ? "https://github.com/owner/repo" : "C:\\projects\\my-app"} spellCheck={false} />
        </label>
      )}
      <div className="row">
        <button className="btn" disabled={busy}>{busy ? "Analyzing…" : "Analyze"}</button>
        {busy && <Spinner />}
        {busy && <span className="muted small">Large repositories can take a minute. The code is read, never executed.</span>}
      </div>
      {error && <ErrorNotice message={error} />}
    </form>
  );
}

export function HomePage() {
  const nav = useNavigate();
  const repos = useAsync(() => api.repositories(), []);

  async function remove(id: string) {
    if (!window.confirm("Delete this analysis? The repository itself is not touched.")) return;
    await api.deleteRepository(id);
    repos.reload();
  }

  return (
    <div className="stack">
      <div><h1>Repositories</h1><div className="muted">Analyze a codebase, then explore its graph, ask questions and check the impact of a change.</div></div>
      <AnalyzeForm onDone={(id) => nav(`/repo/${encodeURIComponent(id)}`)} />
      <Async state={repos}>
        {(list) => list.length === 0 ? <div className="empty card">No repositories analyzed yet.</div> : (
          <div className="grid cols-2">
            {list.map((r) => (
              <div key={r.repo_id} className="card stack" style={{ gap: 8 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h2 style={{ margin: 0 }}><Link to={`/repo/${encodeURIComponent(r.repo_id)}`}>{r.name}</Link></h2>
                  <button className="btn secondary small" onClick={() => remove(r.repo_id)} aria-label={`Delete ${r.name}`}>Delete</button>
                </div>
                <div className="muted small mono" style={{ overflow: "hidden", textOverflow: "ellipsis" }} title={r.origin}>{r.origin}</div>
                <div className="chips">
                  {Object.entries(r.language_counts).map(([l, n]) => <Badge key={l}>{l} · {n}</Badge>)}
                </div>
                <div className="small muted">{r.entity_count.toLocaleString()} entities · {r.relationship_count.toLocaleString()} relationships · {r.source_file_count} source files</div>
              </div>
            ))}
          </div>
        )}
      </Async>
    </div>
  );
}
