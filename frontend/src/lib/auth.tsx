// Auth state for optional Team mode (PRD-07). In solo mode (the default) this is
// inert: `teamMode` is false, `user` is null, and no login wall is ever shown.

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { apiJson, apiPost } from "@/lib/api";

export type Role = "owner" | "admin" | "member" | "viewer";

export interface User {
  id: string;
  username: string;
  role: Role;
  status: string;
  createdAt: string;
  lastLoginAt: string | null;
}

interface MeResponse {
  teamMode: boolean;
  user: User | null;
}

interface AuthState {
  loading: boolean;
  teamMode: boolean;
  user: User | null;
  /** Re-probe GET /api/auth/me. */
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

const ROLE_RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };
export function atLeast(role: Role | undefined, min: Role): boolean {
  return role != null && ROLE_RANK[role] >= ROLE_RANK[min];
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [teamMode, setTeamMode] = useState(false);
  const [user, setUser] = useState<User | null>(null);

  const refresh = useCallback(async () => {
    try {
      const me = await apiJson<MeResponse>("/api/auth/me");
      setTeamMode(me.teamMode);
      setUser(me.user);
    } catch {
      // 401 in Team mode -> authenticated session required but absent.
      setTeamMode(true);
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    await apiPost("/api/auth/login", { username, password });
    await refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    await apiPost("/api/auth/logout", {}).catch(() => {});
    await refresh();
  }, [refresh]);

  return (
    <AuthContext.Provider value={{ loading, teamMode, user, refresh, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
