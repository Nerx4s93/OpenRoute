import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "./auth/AuthProvider";
import { AppShell } from "./components/AppShell";
import { DisabledPage, FullPageError, FullPageLoader } from "./components/FullPage";
import { KeysPage } from "./pages/KeysPage";
import { LoginPage } from "./pages/LoginPage";
import { ProxiesPage } from "./pages/ProxiesPage";

function ProtectedRoute({ children }: { children: ReactNode }) {
  const { session, loading, error, disabled, retrySession } = useAuth();
  const location = useLocation();

  if (loading) return <FullPageLoader />;
  if (disabled) return <DisabledPage message={disabled} />;
  if (error) return <FullPageError message={error.message} onRetry={retrySession} />;
  if (!session?.authenticated) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }
  return <>{children}</>;
}

function RootRedirect() {
  const { session, loading, error, disabled, retrySession } = useAuth();

  if (loading) return <FullPageLoader />;
  if (disabled) return <DisabledPage message={disabled} />;
  if (error) return <FullPageError message={error.message} onRetry={retrySession} />;
  return <Navigate to={session?.authenticated ? "/admin/ui" : "/login"} replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<RootRedirect />} />
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/admin/ui"
        element={
          <ProtectedRoute>
            <AppShell>
              <ProxiesPage />
            </AppShell>
          </ProtectedRoute>
        }
      />
      <Route
        path="/admin/ui/keys"
        element={
          <ProtectedRoute>
            <AppShell>
              <KeysPage />
            </AppShell>
          </ProtectedRoute>
        }
      />
      <Route path="*" element={<Navigate to="/admin/ui" replace />} />
    </Routes>
  );
}
