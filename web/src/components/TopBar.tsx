import { useAuth } from "../auth";
import type { FeedStatus } from "../ws";
import type { Tab } from "../App";

const TABS: { id: Tab; label: string }[] = [
  { id: "processes", label: "Processes" },
  { id: "scheduler", label: "Scheduler" },
  { id: "audit", label: "Audit" },
  { id: "approvals", label: "Approvals" },
  { id: "files", label: "Files" },
  { id: "providers", label: "LLM" },
];

export function TopBar({
  tab,
  onTab,
  feedStatus,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  feedStatus: FeedStatus;
}) {
  const { principal, logout } = useAuth();
  return (
    <header className="topbar">
      <div className="brand">
        aios <span className="muted">control</span>
      </div>
      <nav>
        {TABS.map((t) => (
          <button
            key={t.id}
            className={tab === t.id ? "active" : undefined}
            onClick={() => onTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <div className="topbar-right">
        <span className={`feed-dot ${feedStatus}`} title={`live feed: ${feedStatus}`} />
        <span>
          {principal?.name} · {principal?.role}
        </span>
        <button className="ghost small" onClick={logout}>
          sign out
        </button>
      </div>
    </header>
  );
}