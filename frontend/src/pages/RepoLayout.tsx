import { Outlet, useOutletContext, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { RepoSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { Async } from "../components/ui";

export interface RepoCtx { id: string; repo: RepoSummary }
export const useRepo = () => useOutletContext<RepoCtx>();

/** Loads the repository once; every tab below receives it through the outlet context. */
export function RepoLayout() {
  const { id = "" } = useParams();
  const state = useAsync(() => api.repository(id), [id]);
  return (
    <Async state={state}>
      {(repo) => (
        <>
          <div className="page-head">
            <div>
              <h1>{repo.name}</h1>
              <div className="muted small mono" title={repo.origin}>{repo.origin}</div>
            </div>
          </div>
          <Outlet context={{ id, repo } satisfies RepoCtx} />
        </>
      )}
    </Async>
  );
}
