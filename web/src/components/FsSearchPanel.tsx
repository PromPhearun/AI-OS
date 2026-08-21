import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { AgentRecord, FsHit } from "../types";

export function FsSearchPanel({ processes }: { processes: AgentRecord[] }) {
  const live = processes.filter((r) => r.state !== "terminated");
  const [pid, setPid] = useState<number | null>(live[0]?.pid ?? null);
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [hits, setHits] = useState<FsHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim() || pid === null) return;
    setSearching(true);
    setErr(null);
    try {
      const res = await api.fsSearch(query.trim(), pid, topK);
      setHits(res.hits);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "search failed");
      setHits([]);
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">semantic fs search</div>
      <div className="panel-body">
        <form className="form-row" onSubmit={(e) => void run(e)}>
          <select
            value={pid ?? ""}
            onChange={(e) => setPid(Number(e.target.value))}
            disabled={live.length === 0}
          >
            {live.length === 0 && <option value="">no live agents</option>}
            {live.map((r) => (
              <option key={r.pid} value={r.pid}>
                {r.pid} · {r.name}
              </option>
            ))}
          </select>
          <input
            style={{ flex: 1 }}
            placeholder="semantic query over the agent's indexed artifacts"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
            {[3, 5, 10, 20].map((k) => (
              <option key={k} value={k}>
                top {k}
              </option>
            ))}
          </select>
          <button className="primary" type="submit" disabled={searching || !query.trim() || pid === null}>
            {searching ? "…" : "search"}
          </button>
        </form>
        {err && <p className="error-text">{err}</p>}
        {hits.length === 0 && !err && (
          <p className="muted">run a query — hit artifacts are ranked by meaning, not path.</p>
        )}
        {hits.length > 0 && (
          <table>
            <thead>
              <tr>
                <th className="num">score</th>
                <th>path</th>
                <th>mime</th>
                <th>snippet</th>
                <th>created</th>
              </tr>
            </thead>
            <tbody>
              {hits.map((h) => (
                <tr key={h.artifact_id}>
                  <td className="num">{h.score.toFixed(4)}</td>
                  <td className="mono">{h.path}</td>
                  <td className="muted">{h.mime}</td>
                  <td className="muted">{h.snippet}</td>
                  <td className="num">{new Date(h.created_at * 1000).toLocaleTimeString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}