import { NavLink, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "../auth/AuthProvider";
import { usePeriod } from "../hooks/usePeriod";
import { useI18n } from "../i18n";
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
  const { language, t } = useI18n();
  const toast = useToast();
  const location = useLocation();
  const pageTitle = location.pathname.startsWith("/admin/keys")
    ? t("page.keys")
    : location.pathname.startsWith("/admin/proxies")
      ? t("page.proxies")
      : location.pathname.startsWith("/admin/settings")
        ? t("page.settings")
        : t("page.stats");
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
            <span className="brand-sub">{t("app.adminConsole")}</span>
          </span>
        </div>
        <div className="nav-label">{t("app.navManagement")}</div>
        <nav>
          <NavLink
            to={`/admin/proxies${search}`}
            className={(state) => `nav-proxies ${navClass(state)}`}
          >
            {t("app.navProxies")}
          </NavLink>
          <NavLink
            to={`/admin/stats${search}`}
            end
            className={(state) => `nav-stats ${navClass(state)}`}
          >
            {t("app.navStats")}
          </NavLink>
          <NavLink
            to={`/admin/keys${search}`}
            className={(state) => `nav-keys ${navClass(state)}`}
          >
            {t("app.navKeys")}
          </NavLink>
        </nav>
        <div className="sidebar-bottom">
          <nav>
            <NavLink
              to="/admin/settings"
              className={(state) => `nav-settings ${navClass(state)}`}
            >
              {t("app.navSettings")}
            </NavLink>
          </nav>
          <button type="button" onClick={onLogout} disabled={logout.isPending}>
            <span aria-hidden="true">↗</span>
            <span className="logout-label">
              {logout.isPending ? t("app.logoutPending") : t("app.logout")}
            </span>
          </button>
        </div>
      </header>
      <main className="main">
        <div className="topline">
          <div className="breadcrumb">
            <span className="crumb-root">{t("app.breadcrumbRoot")} &nbsp;/&nbsp;</span>
            <strong>{pageTitle}</strong>
          </div>
          <div className="top-meta">
            <span className={`live-dot ${connected ? "" : "off"}`} aria-hidden="true" />
            <span className="meta-label">{t("app.dataAsOf")}</span>
            <span id="srv-now">{formatServerTime(serverTime, language)}</span>
          </div>
        </div>
        {children}
      </main>
    </div>
  );
}
