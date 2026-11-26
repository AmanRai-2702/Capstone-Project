import type { Answer, Contract, EntityDetail, EntityRow, GraphData, ImpactReport, Job, LlmStatus, Overview, RepoSummary,
  SourceLines, TraceResult, UnresolvedItem } from "./types";

/** An API failure carrying the server's own message (the backend never echoes secrets). */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Cannot reach the RepoMind API. Is the backend running?");
  }
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch { /* keep statusText */ }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

type Params = Record<string, string | number | boolean | undefined | null>;
export const query = (params: Params): string => {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") p.set(k, String(v)); });
  const s = p.toString();
  return s ? `?${s}` : "";
};
const post = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const R = (id: string) => `/api/repositories/${encodeURIComponent(id)}`;

export const api = {
  llmStatus: () => request<LlmStatus>("/api/llm/status"),
  repositories: () => request<RepoSummary[]>("/api/repositories"),
  repository: (id: string) => request<RepoSummary>(R(id)),
  deleteRepository: (id: string) => request<{ deleted: string }>(R(id), { method: "DELETE" }),
  analyze: (location: string, exclude: string[] = []) => request<Job>("/api/repositories?background=true", post({ location, exclude })),
  job: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`),
  upload: (file: File) => request<RepoSummary>(`/api/repositories/upload${query({ name: file.name })}`, { method: "POST", body: file }),
  overview: (id: string) => request<Overview>(`${R(id)}/overview`),
  graph: (id: string, p: Params) => request<GraphData>(`${R(id)}/graph${query(p)}`),
  neighborhood: (id: string, entity: string, p: Params = {}) => request<GraphData>(`${R(id)}/graph/neighborhood${query({ id: entity, ...p })}`),
  edge: (id: string, source: string, type: string, target: string) =>
    request<{ evidence?: { snippet?: string; location?: { file_path: string; start_line: number; end_line: number } | null }[] }>(
      `${R(id)}/graph/edge${query({ source, type, target })}`),
  entities: (id: string, p: Params) => request<EntityRow[]>(`${R(id)}/entities${query(p)}`),
  entity: (id: string, entity: string) => request<EntityDetail>(`${R(id)}/entity${query({ id: entity })}`),
  unresolved: (id: string) => request<{ counts: Record<string, number>; items: UnresolvedItem[] }>(`${R(id)}/unresolved`),
  trace: (id: string, source: string, target: string, view: string, maxDepth = 10) =>
    request<TraceResult>(`${R(id)}/trace`, post({ source, target, view, max_depth: maxDepth })),
  impact: (id: string, body: { target?: string; diff?: string; max_depth?: number; strict_references?: boolean }) =>
    request<ImpactReport>(`${R(id)}/impact`, post(body)),
  contracts: (id: string, onlyIssues = false) =>
    request<{ endpoints: Contract[]; issue_count: number }>(`${R(id)}/contracts${query({ only_issues: onlyIssues })}`),
  ask: (id: string, question: string) => request<Answer>(`${R(id)}/ask`, post({ question })),
  source: (id: string, file: string, start = 1, end?: number) => request<SourceLines>(`${R(id)}/source${query({ file, start, end })}`),
};
