export interface RepoSummary {
  repo_id: string; name: string; origin: string;
  file_count: number; source_file_count: number;
  entity_count: number; relationship_count: number;
  language_counts: Record<string, number>;
  files_by_category: Record<string, number>;
  entity_counts_by_type: Record<string, number>;
  relationship_counts_by_type: Record<string, number>;
  relationship_counts_by_kind: Record<string, number>;
  unresolved_counts_by_reason: Record<string, number>;
  unresolved_external_references: number;
  analysis_errors: string[];
  skipped_files: Record<string, number>;
}
export interface Job { job_id: string; status: "running" | "done" | "error"; repo_id: string | null; error: string | null }
export interface GraphNode { id: string; label: string; type: string; language: string; location: string | null; degree: number }
export interface GraphEdge { id?: string; source: string; target: string; type: string; kind: string; confidence: number; evidence_count?: number }
export interface GraphData { nodes: GraphNode[]; edges: GraphEdge[]; meta?: { level: string; total_nodes: number; truncated: boolean } }
export interface EntityRow { id: string; name: string; type: string; language: string; location: string | null }
export interface EntityDetail {
  entity: { id: string; name: string; qualified_name?: string; type: string; language: string; docstring?: string | null;
            location?: { file_path: string; start_line: number; end_line: number } | null; metadata: Record<string, unknown> };
  outgoing: GraphEdge[]; incoming: GraphEdge[];
}
export interface Hop { from: string; to: string; type: string | null; kind: string | null; confidence: number | null; reversed: boolean; derived: boolean }
export interface PathSummary { nodes: string[]; hops: Hop[]; confidence: number; uncertain: boolean }
export interface TraceResult { source: string; target: string; view: string; ambiguous: Record<string, string[]>; paths: PathSummary[] }
export interface ImpactHop { dependent: string; dependency: string; relation: string; kind: string; confidence: number; evidence: string; location: string | null }
export interface ImpactedComponent {
  entity_id: string; name: string; type: string; language: string; location: string | null; distance: number;
  category: string; uncertain: boolean; confidence: number; has_inferred_hop: boolean; chain: ImpactHop[]; explanation: string;
}
export interface RiskFactor { factor: string; severity: string; detail: string; evidence: string[] }
export interface TestSuggestion { file: string; score: number; reasons: string[] }
export interface ImpactReport {
  target: string; changed: string[]; direct: ImpactedComponent[]; transitive: ImpactedComponent[]; blast_radius: number;
  by_language: Record<string, number>; by_type: Record<string, number>; by_category: Record<string, number>;
  affected_entry_points: string[]; suggested_tests: string[]; risk: "low" | "medium" | "high"; risk_reason: string;
  risk_factors: RiskFactor[]; suggested_test_details: TestSuggestion[]; change_kinds: Record<string, string>; notes: string[];
}
export interface ContractIssue { severity: string; type: string; client: string | null; key: string; location: string | null; detail: string }
export interface Contract {
  endpoint: string; method: string | null; route: string | null; handlers: string[];
  response_keys: string[] | null; response_complete: boolean; request_fields: Record<string, boolean> | null;
  clients: { client: string; sent_keys: string[] | null; read_keys: string[] | null; kind: string; confidence: number; location: string | null }[];
  issues: ContractIssue[];
}
export interface Answer {
  question: string; intent: string; answer: string; grounded: boolean; citations: string[]; entities: string[]; warnings: string[];
  provider: string; model: string; requested_provider: string; fallback: boolean; fallback_reason: string;
  retrieval_ms: number; generation_ms: number; prompt_tokens: number; completion_tokens: number;
}
export interface LlmStatus { provider: string; model: string; requested_provider: string; fallback: boolean; fallback_reason: string; external_llm_allowed: boolean }
export interface Overview { headline: string; facts: { text: string; citation: string | null; snippet: string }[] }
export interface SourceLines { file: string; start: number; end: number; lines: string[] }
export interface UnresolvedItem { source_id: string; target_id: string; type: string; reason: string }
