import { ErrorBanner } from "./Feedback";

export function FullPageLoader() {
  return (
    <div className="full-page">
      <div className="full-page-card">
        <span className="live-dot" />
        <span>Загрузка панели…</span>
      </div>
    </div>
  );
}

export function DisabledPage({ message }: { message: string }) {
  return (
    <div className="full-page">
      <div className="full-page-card">
        <span className="eyebrow">Панель выключена</span>
        <h1>Админка недоступна</h1>
        <p className="muted">{message}</p>
        <p className="form-hint">
          Задайте <code>ADMIN_PASSWORD</code> в <code>web-admin/.env</code> и перезапустите
          сервис.
        </p>
      </div>
    </div>
  );
}

export function FullPageError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="full-page">
      <div className="full-page-card">
        <span className="eyebrow">Ошибка</span>
        <h1>Не удалось загрузить панель</h1>
        <ErrorBanner message={message} onRetry={onRetry} />
      </div>
    </div>
  );
}
