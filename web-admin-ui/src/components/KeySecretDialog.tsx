import { useEffect, useRef, useState } from "react";

/**
 * One-time display of a generated client key. The plaintext lives only in
 * component state: closing the dialog unmounts the input and it is gone.
 */
export function KeySecretDialog({
  open,
  title,
  keyId,
  secret,
  onClose,
}: {
  open: boolean;
  title: string;
  keyId: string;
  secret: string;
  onClose: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!open) return;
    setCopied(false);
    const timer = window.setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open, secret]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const copy = async () => {
    const input = inputRef.current;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(secret);
      } else {
        input?.focus();
        input?.select();
        document.execCommand("copy");
      }
      setCopied(true);
    } catch {
      input?.focus();
      input?.select();
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal key-secret-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="key-secret-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h2 id="key-secret-title">{title}</h2>
        <div className="modal-body">
          <p className="secret-lead">
            Скопируйте ключ доступа сейчас — после закрытия окна он больше не
            будет показан.
          </p>
          {keyId ? (
            <p className="secret-meta">
              Key ID: <span className="mono">{keyId}</span>
            </p>
          ) : null}
          <div className="secret-row">
            <input
              ref={inputRef}
              className="mono"
              type="text"
              readOnly
              autoComplete="off"
              spellCheck={false}
              aria-label="Ключ доступа"
              value={secret}
              onFocus={(event) => event.currentTarget.select()}
            />
            <button type="button" className="acc" onClick={() => void copy()}>
              {copied ? "Скопировано" : "Скопировать"}
            </button>
          </div>
          <p className="secret-warning">
            Восстановить ключ нельзя. Если он потеряется, останется только
            перевыпустить его.
          </p>
        </div>
        <div className="modal-actions">
          <button type="button" className="accent" onClick={onClose}>
            Готово
          </button>
        </div>
      </div>
    </div>
  );
}
