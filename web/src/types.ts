/** API shapes for the aios control plane (docs/10-ui.md §4). */

export type AgentState =
  | "spawned"
  | "ready"
  | "running"
  | "blocked"
  | "suspended"
  | "terminated";

export interface Usage {
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  tool_calls: number;
  turns: number;
  run_time_s: number;
}

export interface AgentRecord {
  pid: number;
  name: string;
  state: AgentState;
  priority: number;
  parent_pid: number | null;
  group_id: string;
  created_at: number;
  started_at: number | null;
  exit_status: string | null;
  exit_message: string | null;
  checkpoint_id: string | null;
  budgets: Record<string, number>;
  usage: Usage;
}

export interface ReadyRow {
  pid: number;
  name: string;
  priority: number;
  effective_priority: number;
  waited_ms: number;
  current_wait_ms: number;
}

export interface AgentWaitStats {
  name: string;
  state: string;
  priority: number;
  effective_priority: number;
  waited_ms: number;
  current_wait_ms: number;
  avg_wait_ms: number;
  max_wait_ms: number;
  wait_count: number;
  run_time_s: number;
}

export interface SchedulerSnapshot {
  running: number | null;
  ready: ReadyRow[];
  blocked: number[];
  queues: { ready_depth: number; blocked_depth: number; total_live: number };
  utilization: { run_time_s: number; wall_s: number; percent: number };
  stats: {
    dispatches: number;
    preemptions: number;
    avg_wait_ms: number;
    max_wait_ms: number;
  };
  agents: Record<number, AgentWaitStats>;
}

export interface ProviderHealth {
  provider: string;
  state: "healthy" | "degraded" | "down";
  consecutive_failures: number;
  requests: number;
  failures: number;
  last_error: string | null;
  last_success_ts: number | null;
  last_failure_ts: number | null;
}

export interface ApprovalTicket {
  ticket_id: string;
  pid: number;
  tool: string;
  status: string;
  reason?: string;
  created_at: number;
  expires_at?: number;
  [key: string]: unknown;
}

export interface FsHit {
  artifact_id: string;
  path: string;
  mime: string;
  snippet: string;
  score: number;
  created_at: number;
}

export interface FsSearchResult {
  hits: FsHit[];
  count: number;
}

export interface AuditEntry {
  seq: number;
  ts: number;
  event: string;
  prev_hash: string;
  hash: string;
  [key: string]: unknown;
}

export interface AuditVerifyResult {
  valid: boolean;
  entries: number;
  first_bad: number | null;
}

export interface ConsoleLine {
  ts?: number;
  level?: string;
  message?: string;
  [key: string]: unknown;
}

export interface LaunchResponse {
  pid: number;
}