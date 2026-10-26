import { useEffect, useRef, type ReactNode } from "react";

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel = "Удалить",
  pendingLabel = "Удаление…",
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
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;

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
            Отмена
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={tone}
            onClick={onConfirm}
            disabled={pending}
          >
            {pending ? pendingLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
