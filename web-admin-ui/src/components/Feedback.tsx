import type { ReactNode } from "react";

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="error-banner" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Повторить
        </button>
      ) : null}
    </div>
  );
}

export function EmptyRow({ colSpan, children }: { colSpan: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="empty-cell">{children}</td>
    </tr>
  );
}

export function LoadingRows({ colSpan, rows = 4 }: { colSpan: number; rows?: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, index) => (
        <tr key={index} className="skeleton-row" aria-hidden="true">
          <td colSpan={colSpan}>
            <span className="skeleton" />
          </td>
        </tr>
      ))}
    </>
  );
}
