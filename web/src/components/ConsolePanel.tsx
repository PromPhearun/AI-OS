import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "../api";
import { connectConsole } from "../ws";

type Status = "connecting" | "live" | "closed";

interface LogLine {
  ts?: number;
  level?: string;
  message?: string;
  [key: string]: unknown;
}

function renderLine(line: unknown): string {
  if (typeof line === "string") return line;
  const o = line as LogLine;
  if (typeof o?.message === "string") {
    const ts = o.ts ? new Date(o.ts * 1000).toLocaleTimeString() + " " : "";
    return `${ts}[${o.level ?? "log"}] ${o.message}`;
  }
  try {
    return JSON.stringify(line);
  } catch {
    return String(line);
  }
}

export function ConsolePanel({
  pid,
  onClose,
  notify,
}: {
  pid: number;
  onClose: () => void;
  notify: (message: string) => void;
}) {
  const [lines, setLines] = useState<unknown[]>([]);
  const [status, setStatus] = useState<Status>("connecting");
  const [msg, setMsg] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Initial tail from REST, then live stream.
  useEffect(() => {
    setLines([]);
    setStatus("connecting");
    void api.agentLogs(pid).then((res) => setLines(res.lines)).catch(() => undefined);
    const disconnect = connectConsole(pid, () => api.getToken(), (line) => {
      setLines((prev) => [...prev.slice(-499), line]);
    }, setStatus);
    return disconnect;
  }, [pid]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const text = msg.trim();
    if (!text) return;
    try {
      await api.sendMessage(pid, { text });
      setMsg("");
    } catch (e2) {
      notify(e2 instanceof Error ? e2.message : "send failed");
    }
  };

  return (
    <div className="console-drawer">
      <div className="panel-head" style={{ borderBottom: "1px solid var(--border)" }}>
        console — agent {pid}
        <span className={`feed-dot ${status === "live" ? "live" : "reconnecting"}`} />
        <span className="muted">{status}</span>
        <span className="spacer" />
        <button className="ghost small" onClick={onClose}>
          close
        </button>
      </div>
      <div className="console-lines" ref={scrollRef}>
        {lines.length === 0 && <div className="line muted">no log lines yet</div>}
        {lines.map((line, i) => (
          <div key={i} className="line">
            {renderLine(line)}
          </div>
        ))}
      </div>
      <form className="console-input" onSubmit={(e) => void send(e)}>
        <input
          placeholder={`message to agent ${pid} (delivered via mailbox)`}
          value={msg}
          onChange={(e) => setMsg(e.target.value)}
        />
        <button type="submit" disabled={!msg.trim()}>
          send
        </button>
      </form>
    </div>
  );
}