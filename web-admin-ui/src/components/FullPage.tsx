import { useI18n } from "../i18n";
import { ErrorBanner } from "./Feedback";

export function FullPageLoader() {
  const { t } = useI18n();
  return (
    <div className="full-page">
      <div className="full-page-card">
        <span className="live-dot" />
        <span>{t("full.loading")}</span>
      </div>
    </div>
  );
}

export function DisabledPage({ message }: { message: string }) {
  const { t } = useI18n();
  return (
    <div className="full-page">
      <div className="full-page-card">
        <span className="eyebrow">{t("full.disabledEyebrow")}</span>
        <h1>{t("full.disabledTitle")}</h1>
        <p className="muted">{message}</p>
        <p className="form-hint">
          {t("full.disabledHintPrefix")}
          <code>ADMIN_PASSWORD</code>
          {t("full.disabledHintMiddle")}
          <code>web-admin/.env</code>
          {t("full.disabledHintSuffix")}
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
  const { t } = useI18n();
  return (
    <div className="full-page">
      <div className="full-page-card">
        <span className="eyebrow">{t("full.errorEyebrow")}</span>
        <h1>{t("full.errorTitle")}</h1>
        <ErrorBanner message={message} onRetry={onRetry} />
      </div>
    </div>
  );
}
