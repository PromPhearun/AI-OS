import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { AgentRecord } from "../types";

const DEFAULT_SPEC: Record<string, unknown> = {
  name: "researcher",
  version: "1",
  description: "Gathers research notes (launched from the web desktop).",
  group_id: "desktop",
  priority: 0,
  llm: {
    model: "mock",
    system: "You are a research assistant. Be concise.",
    temperature: 0.0,
  },
  budgets: { max_turns: 6, max_tool_calls: 20 },
  capabilities: { tools: [{ name: "fs.write" }] },
};

function fmtEpoch(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export function ProcessesPanel({
  processes,
  selectedPid,
  onSelect,
  notify,
}: {
  processes: AgentRecord[];
  selectedPid: number | null;
  onSelect: (pid: number | null) => void;
  notify: (message: string) => void;
}) {
  const [launchOpen, setLaunchOpen] = useState(false);
  const [specText, setSpecText] = useState(() => JSON.stringify(DEFAULT_SPEC, null, 2));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const act = async (pid: number, action: "suspend" | "resume" | "kill") => {
    try {
      await api.agentAction(pid, action);
      notify(`agent ${pid} ${action}`);
    } catch (e) {
      notify(e instanceof Error ? e.message : `${action} failed`);
    }
  };

  const launch = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const spec = JSON.parse(specText) as Record<string, unknown>;
      const res = await api.launchAgent(spec);
      notify(`launched agent ${res.pid}`);
      setLaunchOpen(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "launch failed");
    } finally {
      setBusy(false);
    }
  };

  const live = processes.filter((r) => r.state !== "terminated");
  const dead = processes.filter((r) => r.state === "terminated");

  return (
    <div className="panel">
      <div className="panel-head">
        Processes
        <span className="muted">({live.length} live · {dead.length} terminated)</span>
        <span className="spacer" />
        <button className="small" onClick={() => setLaunchOpen(true)}>
          launch agent
        </button>
      </div>
      <div className="panel-body">
        <table>
          <thead>
            <tr>
              <th>pid</th>
              <th>name</th>
              <th>state</th>
              <th className="num">prio</th>
              <th className="num">turns</th>
              <th className="num">cost</th>
              <th className="num">run</th>
              <th>spawned</th>
              <th>actions</th>
            </tr>
          </thead>
          <tbody>
            {processes.map((r) => (
              <tr key={r.pid}>
                <td className="num">{r.pid}</td>
                <td>
                  <button className="ghost small" onClick={() => onSelect(selectedPid === r.pid ? null : r.pid)} title="attach console">
                    {r.name} {selectedPid === r.pid ? "▾" : ""}
                  </button>
                </td>
                <td>
                  <span className={`badge ${r.state}`}>{r.state}</span>
                  {r.state === "terminated" && r.exit_status && (
                    <span className="muted"> · {r.exit_status}</span>
                  )}
                </td>
                <td className="num">{r.priority}</td>
                <td className="num">{r.usage.turns}</td>
                <td className="num">${r.usage.cost_usd.toFixed(4)}</td>
                <td className="num">{r.usage.run_time_s.toFixed(1)}s</td>
                <td>{fmtEpoch(r.created_at)}</td>
                <td>
                  {r.state === "running" || r.state === "ready" ? (
                    <button className="small" onClick={() => void act(r.pid, "suspend")}>
                      suspend
                    </button>
                  ) : r.state === "suspended" ? (
                    <button className="small" onClick={() => void act(r.pid, "resume")}>
                      resume
                    </button>
                  ) : null}
                  {r.state !== "terminated" && (
                    <button className="small danger" onClick={() => void act(r.pid, "kill")}>
                      kill
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {processes.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  no agents yet — launch one above
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {launchOpen && (
        <div className="modal-backdrop" onClick={() => setLaunchOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="panel-head">launch agent — spec JSON</div>
            <div className="panel-body">
              <textarea
                rows={18}
                value={specText}
                onChange={(e) => setSpecText(e.target.value)}
                spellCheck={false}
              />
              {err && <p className="error-text">{err}</p>}
            </div>
            <div className="modal-actions">
              <button onClick={() => setLaunchOpen(false)}>cancel</button>
              <button className="primary" disabled={busy} onClick={(e) => void launch(e)}>
                {busy ? "launching…" : "launch"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}