/** WebSocket clients for the control-plane streams.

 * Uses POST /v1/auth/ws-token to exchange the JWT for a short-lived,
 * single-use token before opening each WebSocket.  Keeps JWTs out of
 * server/proxy access logs.
 */
import type { AgentRecord, AuditEntry, SchedulerSnapshot } from "./types";

/** Fetch a one-time WS handshake token from the control plane. */
async function fetchWsToken(getToken: () => string | null): Promise<string | null> {
  const jwt = getToken();
  if (!jwt) return null;
  try {
    const res = await fetch("/v1/auth/ws-token", {
      method: "POST",
      headers: { Authorization: `Bearer ${jwt}` },
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { ws_token?: string };
    return body.ws_token ?? null;
  } catch {
    return null;
  }
}

function wsUrl(path: string, token: string | null): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${proto}://${location.host}${path}`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

export type FeedMsg =
  | { type: "audit"; data: AuditEntry[] }
  | { type: "scheduler"; data: SchedulerSnapshot }
  | { type: "processes"; data: AgentRecord[] };

export type FeedStatus = "connecting" | "live" | "reconnecting";

/**
 * Subscribe to the global feed. Returns a disconnect callback.
 * Auto-reconnects with exponential backoff; the token getter is consulted on
 * every (re)connect so a fresh JWT is used after expiry.
 */
export function connectFeed(
  getToken: () => string | null,
  onEvent: (msg: FeedMsg) => void,
  onStatus?: (status: FeedStatus) => void,
): () => void {
  let closed = false;
  let ws: WebSocket | null = null;
  let retries = 0;
  let timer: number | undefined;

  const open = async () => {
    if (closed) return;
    onStatus?.(retries > 0 ? "reconnecting" : "connecting");
    const wsToken = await fetchWsToken(getToken);
    if (closed) return;
    ws = new WebSocket(wsUrl("/v1/ws/feed", wsToken));
    ws.onopen = () => {
      retries = 0;
      onStatus?.("live");
    };
    ws.onmessage = (ev) => {
      try {
        onEvent(JSON.parse(ev.data as string) as FeedMsg);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      if (closed) return;
      onStatus?.("reconnecting");
      const delay = Math.min(1000 * 2 ** retries, 15000);
      retries += 1;
      timer = window.setTimeout(open, delay);
    };
    ws.onerror = () => ws?.close();
  };
  open();

  return () => {
    closed = true;
    if (timer !== undefined) window.clearTimeout(timer);
    ws?.close();
  };
}

/** Tail an agent's console log over WS. Returns a disconnect callback. */
export function connectConsole(
  pid: number,
  getToken: () => string | null,
  onLine: (line: unknown) => void,
  onStatus?: (status: "connecting" | "live" | "closed") => void,
): () => void {
  let closed = false;
  let ws: WebSocket | null = null;
  let retries = 0;
  let timer: number | undefined;

  const open = async () => {
    if (closed) return;
    onStatus?.("connecting");
    const wsToken = await fetchWsToken(getToken);
    if (closed) return;
    ws = new WebSocket(wsUrl(`/v1/agents/${pid}/ws/console`, wsToken));
    ws.onopen = () => {
      retries = 0;
      onStatus?.("live");
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as { type?: string; data?: unknown };
        if (msg.type === "console") onLine(msg.data);
      } catch {
        /* ignore */
      }
    };
    ws.onclose = (ev) => {
      onStatus?.("closed");
      // Terminal close codes: 4004 no such agent, 4401 unauthorized. Do not
      // reconnect when the server explicitly rejects us.
      if (closed || ev.code === 4004 || ev.code === 4401) return;
      const delay = Math.min(1000 * 2 ** retries, 15000);
      retries += 1;
      timer = window.setTimeout(open, delay);
    };
    ws.onerror = () => ws?.close();
  };
  open();

  return () => {
    closed = true;
    if (timer !== undefined) window.clearTimeout(timer);
    ws?.close();
  };
}