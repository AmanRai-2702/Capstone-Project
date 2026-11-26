import { useState } from "react";
import { api } from "../api/client";
import { Async, Badge, shortId } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { useRepo } from "./RepoLayout";

export function ContractsPage() {
  const { id } = useRepo();
  const [onlyIssues, setOnlyIssues] = useState(false);
  const state = useAsync(() => api.contracts(id, onlyIssues), [id, onlyIssues]);
  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div><h2 style={{ margin: 0 }}>API contracts</h2>
          <div className="muted">What each endpoint returns and accepts, what its front-end callers send and read, and where they disagree.</div></div>
        <label className="row small"><input type="checkbox" checked={onlyIssues} onChange={(e) => setOnlyIssues(e.target.checked)} /> Only endpoints with issues</label>
      </div>
      <Async state={state}>
        {(d) => d.endpoints.length === 0 ? <div className="card empty">{onlyIssues ? "No contract issues found." : "No API endpoints with a handler or caller were found."}</div> : (
          <>
            <div className={`notice ${d.issue_count ? "" : "info"}`}>{d.issue_count} issue(s) across {d.endpoints.length} endpoint(s).</div>
            {d.endpoints.map((c) => (
              <div key={c.endpoint} className="card stack" style={{ gap: 8 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h3 style={{ margin: 0 }}><span className="badge">{c.method}</span> <span className="mono">{c.route}</span></h3>
                  {c.issues.length > 0 && <Badge tone="high">{c.issues.length} issue(s)</Badge>}
                </div>
                <div className="small muted">Handled by {c.handlers.map(shortId).join(", ") || "(no handler found)"}</div>
                <div className="grid cols-2 small">
                  <div><b>Returns</b>: {c.response_keys?.length ? c.response_keys.map((k) => <code key={k}>{k} </code>) : "unknown"}
                    {!c.response_complete && c.response_keys && <div className="muted">Response is built dynamically; this list may be incomplete.</div>}</div>
                  <div><b>Accepts</b>: {c.request_fields && Object.keys(c.request_fields).length ? Object.entries(c.request_fields).map(([k, req]) => <code key={k}>{k}{req ? "*" : ""} </code>) : "unknown"}</div>
                </div>
                {c.clients.length > 0 && (
                  <table className="t"><thead><tr><th>Caller</th><th>Sends</th><th>Reads</th><th>Link</th></tr></thead><tbody>
                    {c.clients.map((cl, i) => (
                      <tr key={i}><td className="mono small">{shortId(cl.client)}</td><td className="small">{cl.sent_keys ? cl.sent_keys.join(", ") || "—" : <span className="muted">unknown</span>}</td>
                        <td className="small">{cl.read_keys ? cl.read_keys.join(", ") || "—" : <span className="muted">unknown</span>}</td>
                        <td><Badge tone={cl.kind === "detected" ? "detected" : "inferred"}>{cl.kind}</Badge></td></tr>))}
                  </tbody></table>)}
                {c.issues.map((i, k) => (
                  <div key={k} className={`notice ${i.severity === "high" ? "bad" : i.severity === "info" ? "info" : ""}`}>
                    <Badge tone={i.severity}>{i.severity}</Badge> {i.detail} {i.location && <span className="mono small">({i.location})</span>}
                  </div>))}
              </div>
            ))}
          </>
        )}
      </Async>
    </div>
  );
}
