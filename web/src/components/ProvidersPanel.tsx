import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ProviderHealth } from "../types";

function fmtTs(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "—";
}

export function ProvidersPanel() {
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.providers();
      setProviders(res.providers);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "provider fetch failed");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="panel">
      <div className="panel-head">
        LLM providers — failover health
        <span className="muted">(spec.llm.failover walks this list per request)</span>
        <span className="spacer" />
        <button className="small" onClick={() => void refresh()}>
          refresh
        </button>
      </div>
      <div className="panel-body">
        {err && <p className="error-text">{err}</p>}
        {providers.length === 0 && !err && <p className="muted">no providers registered</p>}
        {providers.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>provider</th>
                <th>state</th>
                <th className="num">requests</th>
                <th className="num">failures</th>
                <th className="num">consec</th>
                <th>last error</th>
                <th>last ok</th>
                <th>last fail</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.provider}>
                  <td className="mono">{p.provider}</td>
                  <td>
                    <span className={`badge ${p.state}`}>{p.state}</span>
                  </td>
                  <td className="num">{p.requests}</td>
                  <td className="num">{p.failures}</td>
                  <td className="num">{p.consecutive_failures}</td>
                  <td className="muted">{p.last_error ?? "—"}</td>
                  <td>{fmtTs(p.last_success_ts)}</td>
                  <td>{fmtTs(p.last_failure_ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}