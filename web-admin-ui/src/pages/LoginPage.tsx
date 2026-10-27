import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { DisabledPage, FullPageError, FullPageLoader } from "../components/FullPage";
import { useI18n } from "../i18n";

export function LoginPage() {
  const { session, loading, error, disabled, login, retrySession } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState("");

  if (loading) return <FullPageLoader />;
  if (disabled) return <DisabledPage message={disabled} />;
  if (error) return <FullPageError message={error.message} onRetry={retrySession} />;
  if (session?.authenticated) {
    const state = location.state as { from?: string } | null;
    return <Navigate to={state?.from ?? "/admin/stats"} replace />;
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password || login.isPending) return;
    login.mutate(password, {
      onSuccess: () => {
        const state = location.state as { from?: string } | null;
        navigate(state?.from ?? "/admin/stats", { replace: true });
      },
    });
  };

  const errorText = login.error
    ? login.error.status === 429 && login.error.retryAfter
      ? `${login.error.message}. ${t("login.retryAfter", {
          seconds: login.error.retryAfter,
        })}`
      : login.error.message
    : "";

  return (
    <div className="login-page">
      <aside className="login-visual">
        <div className="login-brand">
          <span className="brand-mark" aria-hidden="true">OR</span> OpenRoute
        </div>
        <div className="login-statement">
          <div className="eyebrow">{t("login.eyebrow")}</div>
          <h2>
            {t("login.headlineLine1")}
            <br />
            {t("login.headlineLine2")}
          </h2>
          <p>{t("login.lead")}</p>
        </div>
        <div className="login-foot">OPENROUTE &nbsp; / &nbsp; CONTROL PLANE</div>
      </aside>
      <main className="login-main">
        <div className="login-wrap">
          <div className="login-box">
            <div className="login-kicker">{t("login.kicker")}</div>
            <h1>OpenRoute Admin</h1>
            <p className="sub">{t("login.subtitle")}</p>
            <form onSubmit={submit}>
              <label htmlFor="password">{t("login.password")}</label>
              <input
                id="password"
                type="password"
                name="password"
                placeholder={t("login.passwordPlaceholder")}
                autoComplete="current-password"
                autoFocus
                required
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                  if (login.error) login.reset();
                }}
                aria-invalid={Boolean(errorText)}
              />
              <button type="submit" disabled={login.isPending || !password}>
                {login.isPending ? t("login.submitPending") : t("login.submit")}
              </button>
            </form>
            {errorText ? <div className="err" role="alert">{errorText}</div> : null}
            <div className="login-help">{t("login.help")}</div>
          </div>
        </div>
      </main>
    </div>
  );
}
