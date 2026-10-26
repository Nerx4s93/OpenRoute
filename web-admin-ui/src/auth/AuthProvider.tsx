import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from "@tanstack/react-query";
import { api, ApiError, setCsrfToken } from "../api/client";
import type { SessionInfo } from "../api/types";

const SESSION_KEY = ["admin", "session"] as const;

interface AuthContextValue {
  session: SessionInfo | undefined;
  loading: boolean;
  error: ApiError | null;
  disabled: string | null;
  login: UseMutationResult<SessionInfo, ApiError, string>;
  logout: UseMutationResult<void, ApiError, void>;
  retrySession: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function clearDataQueries(queryClient: ReturnType<typeof useQueryClient>): void {
  queryClient.removeQueries({ queryKey: ["proxies"] });
  queryClient.removeQueries({ queryKey: ["keys"] });
  queryClient.removeQueries({ queryKey: ["stats"] });
  queryClient.removeQueries({ queryKey: ["px6"] });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

  const sessionQuery = useQuery<SessionInfo, ApiError>({
    queryKey: SESSION_KEY,
    queryFn: () => api.session(),
    retry: false,
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    if (sessionQuery.data?.csrf) setCsrfToken(sessionQuery.data.csrf);
  }, [sessionQuery.data]);

  useEffect(() => {
    const onUnauthorized = () => {
      setCsrfToken("");
      clearDataQueries(queryClient);
      queryClient.setQueryData<SessionInfo>(SESSION_KEY, { authenticated: false });
    };
    window.addEventListener("openroute:unauthorized", onUnauthorized);
    return () => window.removeEventListener("openroute:unauthorized", onUnauthorized);
  }, [queryClient]);

  const login = useMutation<SessionInfo, ApiError, string>({
    mutationFn: (password) => api.login(password),
    onSuccess: (data) => {
      setCsrfToken(data.csrf ?? "");
      clearDataQueries(queryClient);
      queryClient.setQueryData(SESSION_KEY, data);
    },
  });

  const logout = useMutation<void, ApiError, void>({
    mutationFn: () => api.logout(),
    onSuccess: () => {
      setCsrfToken("");
      clearDataQueries(queryClient);
      queryClient.setQueryData<SessionInfo>(SESSION_KEY, { authenticated: false });
    },
    onError: (error) => {
      if (error.status === 401) {
        setCsrfToken("");
        clearDataQueries(queryClient);
        queryClient.setQueryData<SessionInfo>(SESSION_KEY, { authenticated: false });
      }
    },
  });

  const refetchSession = sessionQuery.refetch;
  const retrySession = useCallback(() => {
    void refetchSession();
  }, [refetchSession]);

  const value = useMemo<AuthContextValue>(() => {
    const error = sessionQuery.error ?? null;
    return {
      session: sessionQuery.data,
      loading: sessionQuery.isLoading,
      error,
      disabled: error?.status === 404 ? error.message : null,
      login,
      logout,
      retrySession,
    };
  }, [sessionQuery.data, sessionQuery.isLoading, sessionQuery.error, login, logout, retrySession]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
