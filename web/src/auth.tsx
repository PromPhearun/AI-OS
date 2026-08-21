/** Auth state: API key exchange → JWT, restored from sessionStorage. */
import { createContext, useContext, useState, type ReactNode } from "react";
import { api, type Principal } from "./api";

interface AuthState {
  principal: Principal | null;
  busy: boolean;
  error: string | null;
  login: (apiKey: string) => Promise<void>;
  logout: () => void;
}

const AuthCtx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(() => api.getPrincipal());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const logout = () => {
    api.clearSession();
    setPrincipal(null);
  };

  return (
    <AuthCtx.Provider value={{ principal, busy, error, login, logout }}>
      {children}
    </AuthCtx.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}