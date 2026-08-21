/** Typed REST client for the aios control plane (same-origin /v1). */
import type {
  AgentRecord,
  ApprovalTicket,
  AuditEntry,
  AuditVerifyResult,
  FsSearchResult,
  ProviderHealth,
  SchedulerSnapshot,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const TOKEN_KEY = "aios.jwt";
const PRINCIPAL_KEY = "aios.principal";

export interface Principal {
  name: string;
  role: string;
}

export interface TokenResponse extends Principal {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export class Api {
  private token: string | null = null;

  constructor() {
    this.token = sessionStorage.getItem(TOKEN_KEY);
  }

  get hasToken(): boolean {
    return this.token !== null;
  }

  /** Token getter for WebSocket handshakes (re-read on reconnect). */
  getToken(): string | null {
    return this.token;
  }

  getPrincipal(): Principal | null {
    try {
      const raw = sessionStorage.getItem(PRINCIPAL_KEY);
      return raw ? (JSON.parse(raw) as Principal) : null;
    } catch {
      return null;
    }
  }

  setSession(token: string, principal: Principal): void {
    this.token = token;
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(PRINCIPAL_KEY, JSON.stringify(principal));
  }

  clearSession(): void {
    this.token = null;
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(PRINCIPAL_KEY);
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    let res: Response;
    try {
      res = await fetch(path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch {
      throw new ApiError(0, "E_NET", "control plane unreachable");
    }
    if (!res.ok) {
      let code = "E_HTTP";
      let message = res.statusText;
      try {
        const data = (await res.json()) as { error?: { code?: string; message?: string } };
        code = data?.error?.code ?? code;
        message = data?.error?.message ?? message;
      } catch {
        /* non-JSON error body */
      }
      if (res.status === 401) this.clearSession();
      throw new ApiError(res.status, code, message);
    }
    return (await res.json()) as T;
  }

  // ------------------------------------------------------------- public
  health(): Promise<{ status: string; service: string; agents: number }> {
    return this.request("GET", "/v1/health");
  }

  login(apiKey: string): Promise<TokenResponse> {
    return this.request("POST", "/v1/auth/token", { api_key: apiKey });
  }

  // ------------------------------------------------------------- agents
  listAgents(): Promise<{ agents: AgentRecord[] }> {
    return this.request("GET", "/v1/agents");
  }

  launchAgent(spec: unknown): Promise<{ pid: number }> {
    return this.request("POST", "/v1/agents", { spec });
  }

  agentAction(
    pid: number,
    action: "suspend" | "resume" | "kill",
    reason?: string,
  ): Promise<Record<string, unknown>> {
    return this.request("PATCH", `/v1/agents/${pid}`, { action, reason });
  }

  agentLogs(pid: number, limit = 500): Promise<{ pid: number; lines: unknown[]; count: number }> {
    return this.request("GET", `/v1/agents/${pid}/logs?limit=${limit}`);
  }

  sendMessage(pid: number, body: Record<string, unknown>, type = "direct"): Promise<unknown> {
    return this.request("POST", `/v1/agents/${pid}/messages`, { body, type });
  }

  // ------------------------------------------------------ scheduler / llm
  scheduler(): Promise<SchedulerSnapshot> {
    return this.request("GET", "/v1/scheduler");
  }

  providers(): Promise<{ providers: ProviderHealth[] }> {
    return this.request("GET", "/v1/llm");
  }

  // ------------------------------------------------------------ approvals
  approvals(): Promise<{ tickets: ApprovalTicket[] }> {
    return this.request("GET", "/v1/approvals");
  }

  approveTicket(id: string): Promise<unknown> {
    return this.request("POST", `/v1/approvals/${encodeURIComponent(id)}/approve`);
  }

  denyTicket(id: string): Promise<unknown> {
    return this.request("POST", `/v1/approvals/${encodeURIComponent(id)}/deny`);
  }

  // ------------------------------------------------------------ filesystem
  fsSearch(query: string, pid: number, topK = 5): Promise<FsSearchResult> {
    return this.request("POST", "/v1/fs/search", { query, pid, top_k: topK });
  }

  // ---------------------------------------------------------------- audit
  audit(opts?: { event?: string; limit?: number }): Promise<{ entries: AuditEntry[]; count: number }> {
    const params = new URLSearchParams();
    params.set("limit", String(opts?.limit ?? 200));
    if (opts?.event) params.set("event", opts.event);
    return this.request("GET", `/v1/audit?${params.toString()}`);
  }

  verifyAudit(): Promise<AuditVerifyResult> {
    return this.request("GET", "/v1/audit/verify");
  }
}

export const api = new Api();