import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ApprovalTicket } from "../types";

export function ApprovalsPanel({ notify }: { notify: (message: string) => void }) {
  const [tickets, setTickets] = useState<ApprovalTicket[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.approvals();
      setTickets(res.tickets);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "approvals fetch failed");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const act = async (ticket: ApprovalTicket, action: "approve" | "deny") => {
    try {
      if (action === "approve") await api.approveTicket(ticket.ticket_id);
      else await api.denyTicket(ticket.ticket_id);
      notify(`ticket ${ticket.ticket_id} ${action}d`);
      void refresh();
    } catch (e) {
      notify(e instanceof Error ? e.message : `${action} failed`);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        Approval tickets
        <span className="muted">(operator role required)</span>
        <span className="spacer" />
        <button className="small" onClick={() => void refresh()}>
          refresh
        </button>
      </div>
      <div className="panel-body">
        {err && <p className="error-text">{err}</p>}
        {tickets.length === 0 && !err && <p className="muted">no tickets</p>}
        {tickets.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>ticket</th>
                <th className="num">pid</th>
                <th>tool</th>
                <th>reason</th>
                <th>status</th>
                <th>created</th>
                <th>actions</th>
              </tr>
            </thead>
            <tbody>
              {tickets.map((t) => (
                <tr key={t.ticket_id}>
                  <td className="mono">{t.ticket_id.slice(0, 8)}</td>
                  <td className="num">{t.pid}</td>
                  <td>{t.tool}</td>
                  <td className="muted">{String(t.reason ?? "")}</td>
                  <td>
                    <span className={`badge ${t.status === "pending" ? "blocked" : t.status}`}>
                      {t.status}
                    </span>
                  </td>
                  <td>{new Date(t.created_at * 1000).toLocaleTimeString()}</td>
                  <td>
                    {t.status === "pending" ? (
                      <>
                        <button className="small" onClick={() => void act(t, "approve")}>
                          approve
                        </button>{" "}
                        <button className="small danger" onClick={() => void act(t, "deny")}>
                          deny
                        </button>
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}