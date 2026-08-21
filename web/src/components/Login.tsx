import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";

export function Login() {
  const { login, loginWithOidc, busy, error, oidcAvailable, oidcIssuer } = useAuth();
  const [key, setKey] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (key.trim() && !busy) void login(key.trim());
  };

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>aios</h1>
        <p className="muted">control plane — sign in with an operator API key</p>
        <input
          autoFocus
          type="password"
          placeholder="API key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          autoComplete="current-password"
        />
        <button className="primary" type="submit" disabled={busy || !key.trim()}>
          {busy ? "signing in…" : "sign in"}
        </button>
        {oidcAvailable && (
          <>
            <div className="login-divider" aria-hidden="true">
              <span>or continue with single sign-on</span>
            </div>
            <button
              className="sso"
              type="button"
              onClick={() => void loginWithOidc()}
              disabled={busy}
            >
              {busy ? "redirecting…" : "sign in with SSO"}
            </button>
            {oidcIssuer && <p className="hint">identity provider: {oidcIssuer}</p>}
          </>
        )}
        {error && <p className="error">{error}</p>}
        <p className="hint">
          No AIOS_API_KEYS configured? The dev key is <code>dev-key</code> (operator role).
        </p>
      </form>
    </div>
  );
}