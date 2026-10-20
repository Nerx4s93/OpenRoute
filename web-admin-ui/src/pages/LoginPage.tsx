import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { DisabledPage, FullPageError, FullPageLoader } from "../components/FullPage";

export function LoginPage() {
  const { session, loading, error, disabled, login, retrySession } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState("");

  if (loading) return <FullPageLoader />;
  if (disabled) return <DisabledPage message={disabled} />;
  if (error) return <FullPageError message={error.message} onRetry={retrySession} />;
  if (session?.authenticated) {
    const state = location.state as { from?: string } | null;
    return <Navigate to={state?.from ?? "/admin/ui"} replace />;
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password || login.isPending) return;
    login.mutate(password, {
      onSuccess: () => {
        const state = location.state as { from?: string } | null;
        navigate(state?.from ?? "/admin/ui", { replace: true });
      },
    });
  };

  const errorText = login.error
    ? login.error.status === 429 && login.error.retryAfter
      ? `${login.error.message}. Повторите через ${login.error.retryAfter} с.`
      : login.error.message
    : "";

  return (
    <div className="login-page">
      <aside className="login-visual">
        <div className="login-brand">
          <span className="brand-mark" aria-hidden="true">OR</span> OpenRoute
        </div>
        <div className="login-statement">
          <div className="eyebrow">Административная панель</div>
          <h2>Каждый маршрут<br />под контролем.</h2>
          <p>Единое пространство для управления прокси, ключами и активностью сервиса.</p>
        </div>
        <div className="login-foot">OPENROUTE &nbsp; / &nbsp; CONTROL PLANE</div>
      </aside>
      <main className="login-main">
        <div className="login-wrap">
          <div className="login-box">
            <div className="login-kicker">Защищённый доступ</div>
            <h1>OpenRoute Admin</h1>
            <p className="sub">Введите пароль администратора, чтобы продолжить.</p>
            <form onSubmit={submit}>
              <label htmlFor="password">Пароль</label>
              <input
                id="password"
                type="password"
                name="password"
                placeholder="Введите пароль"
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
                {login.isPending ? "Вход…" : "Войти в панель   ↗"}
              </button>
            </form>
            {errorText ? <div className="err" role="alert">{errorText}</div> : null}
            <div className="login-help">Доступ к панели защищён серверной сессией</div>
          </div>
        </div>
      </main>
    </div>
  );
}
