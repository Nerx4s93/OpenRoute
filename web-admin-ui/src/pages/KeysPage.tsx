import { useState, type FormEvent } from "react";
import { PERIOD_SECTIONS } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { KeySecretDialog } from "../components/KeySecretDialog";
import { PeriodTabs } from "../components/PeriodTabs";
import { Section } from "../components/Section";
import { StatusBadge } from "../components/StatusBadge";
import { useAddKey, useDeleteKey, useRotateKey, useSetKey } from "../hooks/useAdminMutations";
import { useKeysLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import { formatLimit, formatModels, formatNumber } from "../lib/format";
import { keyStateLabel } from "../lib/proxyState";

interface KeyForm {
  key_id: string;
  rpm: string;
  tpm: string;
  models: string;
}

interface IssuedKey {
  title: string;
  keyId: string;
  secret: string;
}

const EMPTY_FORM: KeyForm = {
  key_id: "",
  rpm: "",
  tpm: "",
  models: "",
};

function parseLimit(value: string, field: "RPM" | "TPM"): { value: number | null; error?: string } {
  const trimmed = value.trim();
  if (!trimmed) return { value: null };
  if (!/^\d+$/.test(trimmed)) {
    return { value: null, error: `Некорректный ${field}: ${trimmed}` };
  }
  return { value: Number(trimmed) };
}

export function KeysPage() {
  const [period, setPeriod] = usePeriod();
  const query = useKeysLive(period);
  const addKey = useAddKey();
  const rotateKey = useRotateKey();
  const setKey = useSetKey();
  const deleteKey = useDeleteKey();
  const [form, setForm] = useState<KeyForm>(EMPTY_FORM);
  const [formError, setFormError] = useState("");
  const [pendingDelete, setPendingDelete] = useState<{ hash: string; keyId: string } | null>(null);
  const [pendingRotate, setPendingRotate] = useState<{ hash: string; keyId: string } | null>(null);
  const [issuedKey, setIssuedKey] = useState<IssuedKey | null>(null);
  const snapshot = query.data;

  const update = <K extends keyof KeyForm>(field: K, value: KeyForm[K]) => {
    setForm((current) => ({ ...current, [field]: value }));
    if (formError) setFormError("");
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (addKey.isPending) return;

    const rpm = parseLimit(form.rpm, "RPM");
    if (rpm.error) {
      setFormError(rpm.error);
      return;
    }
    const tpm = parseLimit(form.tpm, "TPM");
    if (tpm.error) {
      setFormError(tpm.error);
      return;
    }

    setFormError("");
    addKey.mutate(
      {
        key_id: form.key_id.trim(),
        rpm: rpm.value,
        tpm: tpm.value,
        models: form.models
          .split(",")
          .map((model) => model.trim())
          .filter(Boolean),
      },
      {
        onSuccess: (data) => {
          setForm(EMPTY_FORM);
          setIssuedKey({
            title: "Ключ создан",
            keyId: data.key_id,
            secret: data.key,
          });
          addKey.reset(); // keep the one-time secret out of React Query's cache
        },
      },
    );
  };

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">Доступ / управление</div>
          <h1>Клиентские ключи</h1>
        </div>
      </div>

      <Section id="add-title" icon="＋" title="Добавить клиентский ключ" note="Новый доступ">
        <div className="form-content">
          <form className="add" onSubmit={submit}>
            <div className="field">
              <label htmlFor="key_id">Key ID</label>
              <input
                id="key_id"
                name="key_id"
                type="text"
                placeholder="Генерируется автоматически"
                autoComplete="off"
                value={form.key_id}
                onChange={(event) => update("key_id", event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="rpm">RPM</label>
              <input
                id="rpm"
                name="rpm"
                type="text"
                inputMode="numeric"
                placeholder="Без лимита"
                autoComplete="off"
                value={form.rpm}
                onChange={(event) => update("rpm", event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="tpm">TPM</label>
              <input
                id="tpm"
                name="tpm"
                type="text"
                inputMode="numeric"
                placeholder="Без лимита"
                autoComplete="off"
                value={form.tpm}
                onChange={(event) => update("tpm", event.target.value)}
              />
            </div>
            <div className="field wide full">
              <label htmlFor="models">Доступные модели</label>
              <input
                id="models"
                name="models"
                type="text"
                placeholder="Через запятую; пусто — все модели"
                autoComplete="off"
                value={form.models}
                onChange={(event) => update("models", event.target.value)}
              />
            </div>
            <div className="form-footer key-form-footer">
              <button type="submit" disabled={addKey.isPending}>
                {addKey.isPending ? "Создание…" : "＋   Создать ключ"}
              </button>
            </div>
          </form>
          {formError ? <div className="err" role="alert">{formError}</div> : null}
        </div>
      </Section>

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? "Не удалось загрузить данные"}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      <Section id="keys-title" icon="◇" title="Клиентские ключи" note="Управление доступом">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Key ID</th>
                <th>RPM</th>
                <th>TPM</th>
                <th>Модели</th>
                <th>Статус</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={6} />
              ) : snapshot && snapshot.keys.length ? (
                snapshot.keys.map((key) => {
                  const state = keyStateLabel(key.enabled ? "active" : "revoked");
                  const pending =
                    setKey.isPending && setKey.variables?.keyHash === key.key_hash;
                  const rotating =
                    rotateKey.isPending && rotateKey.variables === key.key_hash;
                  return (
                    <tr key={key.key_hash} className={key.enabled ? undefined : "dead"}>
                      <td className="mono">{key.key_id}</td>
                      <td>{formatLimit(key.rpm)}</td>
                      <td>{formatLimit(key.tpm)}</td>
                      <td className="mono">{formatModels(key.models)}</td>
                      <td><StatusBadge label={state.label} tone={state.tone} /></td>
                      <td className="actions">
                        <button
                          type="button"
                          className="acc"
                          disabled={rotating}
                          onClick={() => setPendingRotate({ hash: key.key_hash, keyId: key.key_id })}
                        >
                          {rotating ? "…" : "Перевыпустить"}
                        </button>
                        <button
                          type="button"
                          className={key.enabled ? undefined : "acc"}
                          disabled={pending}
                          onClick={() => setKey.mutate({ keyHash: key.key_hash, enabled: !key.enabled })}
                        >
                          {pending ? "…" : key.enabled ? "Отозвать" : "Включить"}
                        </button>
                        <button
                          type="button"
                          className="danger"
                          disabled={deleteKey.isPending}
                          onClick={() => setPendingDelete({ hash: key.key_hash, keyId: key.key_id })}
                        >
                          Удалить
                        </button>
                      </td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={6}>Ключей нет — добавьте выше</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <PeriodTabs active={period} onChange={setPeriod} />

      <Section
        id="stats-title"
        icon="↗"
        title="Статистика по ключам"
        note={PERIOD_SECTIONS[period]}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Key ID</th>
                <th>Статус</th>
                <th>Запросы</th>
                <th>Ошибки</th>
                <th>Токены</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={5} />
              ) : snapshot && snapshot.stats.length ? (
                snapshot.stats.map((stat) => {
                  const state = keyStateLabel(stat.state);
                  return (
                    <tr key={stat.key_id}>
                      <td className="mono">{stat.key_id}</td>
                      <td><StatusBadge label={state.label} tone={state.tone} /></td>
                      <td>{formatNumber(stat.requests)}</td>
                      <td>{formatNumber(stat.errors)}</td>
                      <td>{formatNumber(stat.tokens)}</td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={5}>Запросов ещё не было</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <ConfirmDialog
        open={Boolean(pendingRotate)}
        title={`Перевыпустить ключ ${pendingRotate?.keyId ?? ""}?`}
        confirmLabel="Перевыпустить"
        pendingLabel="Перевыпуск…"
        tone="accent"
        pending={rotateKey.isPending}
        onCancel={() => setPendingRotate(null)}
        onConfirm={() => {
          if (!pendingRotate) return;
          rotateKey.mutate(pendingRotate.hash, {
            onSuccess: (data) => {
              setPendingRotate(null);
              setIssuedKey({
                title: "Ключ перевыпущен",
                keyId: data.key_id,
                secret: data.key,
              });
              rotateKey.reset(); // keep the one-time secret out of React Query's cache
            },
          });
        }}
      >
        <p>Старый ключ сразу перестанет работать.</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title={`Удалить ключ ${pendingDelete?.keyId ?? ""}?`}
        pending={deleteKey.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          deleteKey.mutate(pendingDelete.hash, {
            onSuccess: () => setPendingDelete(null),
          });
        }}
      >
        <p>Ключ будет удалён без возможности восстановления. Статистика по нему останется в журнале.</p>
      </ConfirmDialog>

      <KeySecretDialog
        open={Boolean(issuedKey)}
        title={issuedKey?.title ?? ""}
        keyId={issuedKey?.keyId ?? ""}
        secret={issuedKey?.secret ?? ""}
        onClose={() => setIssuedKey(null)}
      />
    </>
  );
}
