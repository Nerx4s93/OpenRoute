import { NavLink, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "../auth/AuthProvider";
import { usePeriod } from "../hooks/usePeriod";
import { formatServerTime } from "../lib/format";
import { LiveStatusProvider, useLiveStatus } from "./LiveStatus";
import { useToast } from "./Toast";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <LiveStatusProvider>
      <ShellLayout>{children}</ShellLayout>
    </LiveStatusProvider>
  );
}

function navClass({ isActive }: { isActive: boolean }): string {
  return isActive ? "active" : "";
}

function ShellLayout({ children }: { children: ReactNode }) {
  const { logout } = useAuth();
  const [period] = usePeriod();
  const { serverTime, connected } = useLiveStatus();
  const toast = useToast();
  const location = useLocation();
  const pageTitle = location.pathname.startsWith("/admin/ui/keys")
    ? "Ключи"
    : location.pathname.startsWith("/admin/ui/proxies")
      ? "Прокси"
      : "Статистика";
  const search = period === "all" ? "" : `?period=${period}`;

  const onLogout = () => {
    logout.mutate(undefined, {
      onError: (error) => toast.push(error.message, "error"),
    });
  };

  return (
    <div className="wrap">
      <header className="top sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">OR</span>
          <span className="brand-name">
            OpenRoute
            <span className="brand-sub">Admin console</span>
          </span>
        </div>
        <div className="nav-label">Управление</div>
        <nav>
          <NavLink
            to={`/admin/ui/proxies${search}`}
            className={(state) => `nav-proxies ${navClass(state)}`}
          >
            Прокси
          </NavLink>
          <NavLink
            to={`/admin/ui${search}`}
            end
            className={(state) => `nav-stats ${navClass(state)}`}
          >
            Статистика
          </NavLink>
          <NavLink
            to={`/admin/ui/keys${search}`}
            className={(state) => `nav-keys ${navClass(state)}`}
          >
            Ключи
          </NavLink>
        </nav>
        <div className="sidebar-bottom">
          <button type="button" onClick={onLogout} disabled={logout.isPending}>
            <span aria-hidden="true">↗</span>
            <span className="logout-label">
              {logout.isPending ? "Выход…" : "Выйти из панели"}
            </span>
          </button>
        </div>
      </header>
      <main className="main">
        <div className="topline">
          <div className="breadcrumb">
            <span className="crumb-root">Панель управления &nbsp;/&nbsp;</span>
            <strong>{pageTitle}</strong>
          </div>
          <div className="top-meta">
            <span className={`live-dot ${connected ? "" : "off"}`} aria-hidden="true" />
            <span className="meta-label">Данные на</span>
            <span id="srv-now">{formatServerTime(serverTime)}</span>
          </div>
        </div>
        {children}
      </main>
    </div>
  );
}
