/** Auth state: API key exchange → JWT, restored from sessionStorage, plus the
 * OIDC/PKCE single sign-on path (Slice 5.3).
 *
 * On boot the provider probes ``/v1/auth/oidc`` to learn whether SSO is
 * configured. If we just returned from the identity provider the callback set
 * a one-time HttpOnly grant cookie; we exchange it for a normal aios JWT
 * exactly once per page load, so the user lands straight in the desktop.
 */
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Principal } from "./api";

interface AuthState {
  principal: Principal | null;
  busy: boolean;
  error: string | null;
  oidcAvailable: boolean;
  oidcIssuer: string | null;
  login: (apiKey: string) => Promise<void>;
  loginWithOidc: () => Promise<void>;
  logout: () => void;
}

const AuthCtx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(() => api.getPrincipal());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [oidcAvailable, setOidcAvailable] = useState(false);
  const [oidcIssuer, setOidcIssuer] = useState<string | null>(null);
  const exchanged = useRef(false);

  // Probe OIDC availability and auto-exchange the post-login grant cookie.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let enabled = false;
      try {
        const status = await api.oidcStatus();
        if (cancelled) return;
        enabled = status.enabled;
        setOidcAvailable(enabled);
        setOidcIssuer(status.issuer ?? null);
      } catch {
        if (!cancelled) setOidcAvailable(false);
      }
      if (!enabled || api.hasToken || exchanged.current || cancelled) return;
      exchanged.current = true;
      try {
        const res = await api.oidcSession();
        if (cancelled) return;
        api.setSession(res.access_token, { name: res.name, role: res.role });
        setPrincipal({ name: res.name, role: res.role });
      } catch {
        /* no grant cookie present — fall through to the login form */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = async (apiKey: string) => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(apiKey);
      api.setSession(res.access_token, { name: res.name, role: res.role });
      setPrincipal({ name: res.name, role: res.role });
    } catch (e) {
      setError(e instanceof Error ? e.message : "login failed");
      api.clearSession();
      setPrincipal(null);
    } finally {
      setBusy(false);
    }
  };

  const loginWithOidc = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.oidcAuthorize();
      // Full top-level navigation (never an iframe): the provider redirects
      // back to the callback, which sets the one-time grant cookie and lands
      // back on the web shell for the auto-exchange above.
      window.location.assign(res.authorize_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : "SSO unavailable");
      setBusy(false);
    }
  };

  const logout = () => {
    api.clearSession();
    setPrincipal(null);
  };

  return (
    <AuthCtx.Provider
      value={{ principal, busy, error, oidcAvailable, oidcIssuer, login, loginWithOidc, logout }}
    >
      {children}
    </AuthCtx.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}