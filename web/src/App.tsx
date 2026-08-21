import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useAuth } from "./auth";
import { connectFeed, type FeedMsg, type FeedStatus } from "./ws";
import type { AgentRecord, AuditEntry, SchedulerSnapshot } from "./types";
import { Login } from "./components/Login";
import { TopBar } from "./components/TopBar";
import { ProcessesPanel } from "./components/ProcessesPanel";
import { SchedulerPanel } from "./components/SchedulerPanel";
import { AuditPanel } from "./components/AuditPanel";
import { ApprovalsPanel } from "./components/ApprovalsPanel";
import { FsSearchPanel } from "./components/FsSearchPanel";
import { ProvidersPanel } from "./components/ProvidersPanel";
import { ConsolePanel } from "./components/ConsolePanel";

export type Tab = "processes" | "scheduler" | "audit" | "approvals" | "files" | "providers";

const AUDIT_CAP = 500;

export function App() {
  const { principal } = useAuth();
  const [tab, setTab] = useState<Tab>("processes");
  const [selectedPid, setSelectedPid] = useState<number | null>(null);
  const [processes, setProcesses] = useState<AgentRecord[]>([]);
  const [scheduler, setScheduler] = useState<SchedulerSnapshot | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [feedStatus, setFeedStatus] = useState<FeedStatus>("connecting");
  const [flash, setFlash] = useState<string | null>(null);
  const flashTimer = useRef<number | undefined>(undefined);

  const notify = useCallback((message: string) => {
    setFlash(message);
    if (flashTimer.current !== undefined) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlash(null), 4000);
  }, []);

  // Live feed drives the process table, scheduler snapshot and audit tail.
  useEffect(() => {
    if (!principal) return;
    const disconnect = connectFeed(
      () => api.getToken(),
      (msg: FeedMsg) => {
        if (msg.type === "processes") setProcesses(msg.data);
        else if (msg.type === "scheduler") setScheduler(msg.data);
        else if (msg.type === "audit") {
          setAudit((prev) => {
            const seen = new Set(prev.map((e) => e.seq));
            const fresh = msg.data.filter((e) => !seen.has(e.seq));
            return [...prev, ...fresh].slice(-AUDIT_CAP);
          });
        }
      },
      setFeedStatus,
    );
    return disconnect;
  }, [principal?.name, principal?.role]);

  // Slow REST fallback while the feed is reconnecting, so the desktop never
  // goes stale (GETs are light; the WS stream is the primary path).
  useEffect(() => {
    if (!principal || feedStatus === "live") return;
    const id = window.setInterval(async () => {
      try {
        const [rows, snap] = await Promise.all([api.listAgents(), api.scheduler()]);
        setProcesses(rows.agents);
        setScheduler(snap);
      } catch {
        /* control plane still down — keep waiting */
      }
    }, 3000);
    return () => window.clearInterval(id);
  }, [principal?.name, principal?.role, feedStatus]);

  if (!principal) return <Login />;

  return (
    <div className="app">
      <TopBar tab={tab} onTab={setTab} feedStatus={feedStatus} />
      {flash && <div className="flash">{flash}</div>}
      <main className="main">
        {tab === "processes" && (
          <ProcessesPanel
            processes={processes}
            selectedPid={selectedPid}
            onSelect={setSelectedPid}
            notify={notify}
          />
        )}
        {tab === "scheduler" && <SchedulerPanel snapshot={scheduler} />}
        {tab === "audit" && <AuditPanel entries={audit} notify={notify} />}
        {tab === "approvals" && <ApprovalsPanel notify={notify} />}
        {tab === "files" && <FsSearchPanel processes={processes} />}
        {tab === "providers" && <ProvidersPanel />}
      </main>
      {selectedPid !== null && (
        <ConsolePanel pid={selectedPid} onClose={() => setSelectedPid(null)} notify={notify} />
      )}
    </div>
  );
}