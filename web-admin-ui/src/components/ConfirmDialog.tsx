import { useEffect, useRef, type ReactNode } from "react";
import { useI18n } from "../i18n";

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  pendingLabel,
  tone = "danger",
  pending = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel?: string;
  pendingLabel?: string;
  tone?: "danger" | "accent";
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;
  const resolvedConfirmLabel = confirmLabel ?? t("common.delete");
  const resolvedPendingLabel = pendingLabel ?? t("common.deleting");

  useEffect(() => {
    if (open) confirmRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pending) cancelRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, pending]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" onMouseDown={() => !pending && onCancel()}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h2 id="confirm-title">{title}</h2>
        <div className="modal-body">{children}</div>
        <div className="modal-actions">
          <button type="button" onClick={onCancel} disabled={pending}>
            {t("common.cancel")}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={tone}
            onClick={onConfirm}
            disabled={pending}
          >
            {pending ? resolvedPendingLabel : resolvedConfirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
