import type { SchedulerSnapshot } from "../types";

export function SchedulerPanel({ snapshot }: { snapshot: SchedulerSnapshot | null }) {
  if (!snapshot) {
    return (
      <div className="panel">
        <div className="panel-head">Scheduler</div>
        <div className="panel-body muted">waiting for the first snapshot…</div>
      </div>
    );
  }

  const { queues, utilization, stats } = snapshot;
  const agents = Object.entries(snapshot.agents).sort((a, b) => Number(a[0]) - Number(b[0]));
  const utilPct = Math.min(utilization.percent, 100);

  return (
    <>
      <div className="metric-grid">
        <div className="metric">
          <div className="label">running</div>
          <div className="value">{snapshot.running ?? "—"}</div>
        </div>
        <div className="metric">
          <div className="label">ready</div>
          <div className="value">{queues.ready_depth}</div>
        </div>
        <div className="metric">
          <div className="label">blocked</div>
          <div className="value">{queues.blocked_depth}</div>
        </div>
        <div className="metric">
          <div className="label">live agents</div>
          <div className="value">{queues.total_live}</div>
        </div>
        <div className="metric">
          <div className="label">utilization</div>
          <div className="value">{utilization.percent.toFixed(1)}%</div>
          <div className="bar" style={{ marginTop: 6 }}>
            <div style={{ width: `${utilPct}%` }} />
          </div>
        </div>
        <div className="metric">
          <div className="label">dispatches</div>
          <div className="value">{stats.dispatches}</div>
        </div>
        <div className="metric">
          <div className="label">preemptions</div>
          <div className="value">{stats.preemptions}</div>
        </div>
        <div className="metric">
          <div className="label">avg wait</div>
          <div className="value">{stats.avg_wait_ms.toFixed(1)}ms</div>
        </div>
        <div className="metric">
          <div className="label">max wait</div>
          <div className="value">{stats.max_wait_ms.toFixed(1)}ms</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">ready queue (effective priority, FIFO within priority)</div>
        <div className="panel-body">
          {snapshot.ready.length === 0 ? (
            <p className="muted">queue empty</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th className="num">pid</th>
                  <th>name</th>
                  <th className="num">prio</th>
                  <th className="num">eff</th>
                  <th className="num">waited</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.ready.map((r) => (
                  <tr key={r.pid}>
                    <td className="num">{r.pid}</td>
                    <td>{r.name}</td>
                    <td className="num">{r.priority}</td>
                    <td className="num">{r.effective_priority}</td>
                    <td className="num">{r.current_wait_ms.toFixed(0)}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">per-agent wait statistics</div>
        <div className="panel-body">
          <table>
            <thead>
              <tr>
                <th className="num">pid</th>
                <th>name</th>
                <th>state</th>
                <th className="num">prio</th>
                <th className="num">eff</th>
                <th className="num">waited</th>
                <th className="num">avg</th>
                <th className="num">max</th>
                <th className="num">count</th>
                <th className="num">run</th>
              </tr>
            </thead>
            <tbody>
              {agents.map(([pid, a]) => (
                <tr key={pid}>
                  <td className="num">{pid}</td>
                  <td>{a.name}</td>
                  <td>
                    <span className={`badge ${a.state}`}>{a.state}</span>
                  </td>
                  <td className="num">{a.priority}</td>
                  <td className="num">{a.effective_priority}</td>
                  <td className="num">{a.current_wait_ms.toFixed(0)}ms</td>
                  <td className="num">{a.avg_wait_ms.toFixed(0)}ms</td>
                  <td className="num">{a.max_wait_ms.toFixed(0)}ms</td>
                  <td className="num">{a.wait_count}</td>
                  <td className="num">{a.run_time_s.toFixed(1)}s</td>
                </tr>
              ))}
              {agents.length === 0 && (
                <tr>
                  <td colSpan={10} className="muted">
                    no live agents
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}