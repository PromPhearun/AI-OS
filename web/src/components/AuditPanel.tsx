import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AuditEntry, AuditVerifyResult } from "../types";

const EVENTS = [
  "control.request",
  "agent.spawn",
  "agent.exit",
  "agent.kill",
  "agent.suspend",
  "agent.resume",
  "agent.log",
  "approval.request",
  "approval.approve",
  "approval.deny",
  "tool.call",
];

function fmtLine(e: AuditEntry): string {
  const time = new Date(e.ts * 1000).toLocaleTimeString();
  const parts = [String(e.seq).padStart(4, "0"), time, e.event];
  if (e.pid !== undefined) parts.push(`pid=${e.pid}`);
  if (e.method && e.path) parts.push(`${e.method} ${e.path}`);
  if (e.status !== undefined) parts.push(`→ ${e.status}`);
  if (e.duration_ms !== undefined) parts.push(`${e.duration_ms}ms`);
  if (e.principal !== undefined) parts.push(`by=${e.principal}`);
  return parts.join("  ");
}

export function AuditPanel({
  entries,
  notify,
}: {
  entries: AuditEntry[];
  notify: (message: string) => void;
}) {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [filter, setFilter] = useState("");
  const [verify, setVerify] = useState<AuditVerifyResult | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const refresh = useCallback(async (event?: string) => {
    try {
      const res = await api.audit({ event: event || undefined, limit: 500 });
      setRows(res.entries);
      setLoadErr(null);
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : "audit fetch failed");
    }
  }, []);

  useEffect(() => {
    void refresh(filter);
  }, [filter, refresh]);

  // Merge live WS entries that arrived after the last REST pull.
  useEffect(() => {
    if (!entries.length) return;
    setRows((prev) => {
      const maxSeq = prev.length ? prev[prev.length - 1].seq : -1;
      const fresh = entries.filter((e) => e.seq > maxSeq);
      return fresh.length ? [...prev, ...fresh].slice(-800) : prev;
    });
  }, [entries]);

  const runVerify = async () => {
    try {
      const res = await api.verifyAudit();
      setVerify(res);
      notify(
        res.valid
          ? `audit chain valid (${res.entries} records)`
          : `audit chain BROKEN at record ${res.first_bad}`,
      );
    } catch (e) {
      setVerify(null);
      notify(e instanceof Error ? e.message : "verify failed");
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        Audit trail
        <span className="spacer" />
        {verify && (
          <span className={verify.valid ? "ok-text" : "error-text"}>
            {verify.valid
              ? `valid · ${verify.entries} records`
              : `broken at ${verify.first_bad}`}
          </span>
        )}
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">all events</option>
          {EVENTS.map((ev) => (
            <option key={ev} value={ev}>
              {ev}
            </option>
          ))}
        </select>
        <button className="small" onClick={() => void runVerify()}>
          verify chain
        </button>
        <button className="small" onClick={() => void refresh(filter)}>
          refresh
        </button>
      </div>
      <div className="panel-body">
        {loadErr && <p className="error-text">{loadErr}</p>}
        {rows.length === 0 && !loadErr && <p className="muted">no audit records</p>}
        <div className="console-lines">
          {rows
            .slice()
            .reverse()
            .map((e) => (
              <div key={e.seq} className="line">
                {fmtLine(e)}
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}