import { useState, type FormEvent } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyRow, ErrorBanner, LoadingRows } from "../components/Feedback";
import { KeySecretDialog } from "../components/KeySecretDialog";
import { PeriodTabs } from "../components/PeriodTabs";
import { Section } from "../components/Section";
import { StatusBadge } from "../components/StatusBadge";
import { useAddKey, useDeleteKey, useRotateKey, useSetKey } from "../hooks/useAdminMutations";
import { useKeysLive } from "../hooks/useLiveSnapshot";
import { usePeriod } from "../hooks/usePeriod";
import { periodSection, useI18n } from "../i18n";
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

function parseLimit(
  value: string,
  field: "RPM" | "TPM",
): { value: number | null; invalid?: { field: "RPM" | "TPM"; value: string } } {
  const trimmed = value.trim();
  if (!trimmed) return { value: null };
  if (!/^\d+$/.test(trimmed)) {
    return { value: null, invalid: { field, value: trimmed } };
  }
  return { value: Number(trimmed) };
}

export function KeysPage() {
  const { language, t } = useI18n();
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
    if (rpm.invalid) {
      setFormError(t("keys.invalidLimit", rpm.invalid));
      return;
    }
    const tpm = parseLimit(form.tpm, "TPM");
    if (tpm.invalid) {
      setFormError(t("keys.invalidLimit", tpm.invalid));
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
            title: t("keys.createdTitle"),
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
          <div className="eyebrow">{t("keys.eyebrow")}</div>
          <h1>{t("keys.title")}</h1>
        </div>
      </div>

      <Section
        id="add-title"
        icon="＋"
        title={t("keys.addTitle")}
        note={t("keys.addNote")}
      >
        <div className="form-content">
          <form className="add" onSubmit={submit}>
            <div className="field">
              <label htmlFor="key_id">{t("keys.keyId")}</label>
              <input
                id="key_id"
                name="key_id"
                type="text"
                placeholder={t("keys.keyIdPlaceholder")}
                autoComplete="off"
                value={form.key_id}
                onChange={(event) => update("key_id", event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="rpm">{t("keys.rpm")}</label>
              <input
                id="rpm"
                name="rpm"
                type="text"
                inputMode="numeric"
                placeholder={t("keys.noLimit")}
                autoComplete="off"
                value={form.rpm}
                onChange={(event) => update("rpm", event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="tpm">{t("keys.tpm")}</label>
              <input
                id="tpm"
                name="tpm"
                type="text"
                inputMode="numeric"
                placeholder={t("keys.noLimit")}
                autoComplete="off"
                value={form.tpm}
                onChange={(event) => update("tpm", event.target.value)}
              />
            </div>
            <div className="field wide full">
              <label htmlFor="models">{t("keys.modelsLabel")}</label>
              <input
                id="models"
                name="models"
                type="text"
                placeholder={t("keys.modelsPlaceholder")}
                autoComplete="off"
                value={form.models}
                onChange={(event) => update("models", event.target.value)}
              />
            </div>
            <div className="form-footer key-form-footer">
              <button type="submit" disabled={addKey.isPending}>
                {addKey.isPending ? t("keys.creating") : t("keys.create")}
              </button>
            </div>
          </form>
          {formError ? <div className="err" role="alert">{formError}</div> : null}
        </div>
      </Section>

      {query.isError ? (
        <ErrorBanner
          message={query.error?.message ?? t("keys.loadFailed")}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      <Section
        id="keys-title"
        icon="◇"
        title={t("keys.listTitle")}
        note={t("keys.listNote")}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t("keys.keyId")}</th>
                <th>{t("keys.rpm")}</th>
                <th>{t("keys.tpm")}</th>
                <th>{t("keys.modelsColumn")}</th>
                <th>{t("keys.status")}</th>
                <th>{t("keys.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={6} />
              ) : snapshot && snapshot.keys.length ? (
                snapshot.keys.map((key) => {
                  const state = keyStateLabel(key.enabled ? "active" : "revoked", language);
                  const pending =
                    setKey.isPending && setKey.variables?.keyHash === key.key_hash;
                  const rotating =
                    rotateKey.isPending && rotateKey.variables === key.key_hash;
                  return (
                    <tr key={key.key_hash} className={key.enabled ? undefined : "dead"}>
                      <td className="mono">{key.key_id}</td>
                      <td>{formatLimit(key.rpm, language)}</td>
                      <td>{formatLimit(key.tpm, language)}</td>
                      <td className="mono">{formatModels(key.models)}</td>
                      <td><StatusBadge label={state.label} tone={state.tone} /></td>
                      <td className="actions">
                        <button
                          type="button"
                          className="acc"
                          disabled={rotating}
                          onClick={() => setPendingRotate({ hash: key.key_hash, keyId: key.key_id })}
                        >
                          {rotating ? "…" : t("keys.reissue")}
                        </button>
                        <button
                          type="button"
                          className={key.enabled ? undefined : "acc"}
                          disabled={pending}
                          onClick={() => setKey.mutate({ keyHash: key.key_hash, enabled: !key.enabled })}
                        >
                          {pending ? "…" : key.enabled ? t("keys.revoke") : t("common.enable")}
                        </button>
                        <button
                          type="button"
                          className="danger"
                          disabled={deleteKey.isPending}
                          onClick={() => setPendingDelete({ hash: key.key_hash, keyId: key.key_id })}
                        >
                          {t("common.delete")}
                        </button>
                      </td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={6}>{t("keys.empty")}</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <PeriodTabs active={period} onChange={setPeriod} />

      <Section
        id="stats-title"
        icon="↗"
        title={t("keys.statsTitle")}
        note={periodSection(period, language)}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t("keys.keyId")}</th>
                <th>{t("keys.status")}</th>
                <th>{t("keys.requests")}</th>
                <th>{t("keys.errors")}</th>
                <th>{t("keys.tokens")}</th>
              </tr>
            </thead>
            <tbody>
              {query.isLoading ? (
                <LoadingRows colSpan={5} />
              ) : snapshot && snapshot.stats.length ? (
                snapshot.stats.map((stat) => {
                  const state = keyStateLabel(stat.state, language);
                  return (
                    <tr key={stat.key_id}>
                      <td className="mono">{stat.key_id}</td>
                      <td><StatusBadge label={state.label} tone={state.tone} /></td>
                      <td>{formatNumber(stat.requests, language)}</td>
                      <td>{formatNumber(stat.errors, language)}</td>
                      <td>{formatNumber(stat.tokens, language)}</td>
                    </tr>
                  );
                })
              ) : (
                <EmptyRow colSpan={5}>{t("keys.noRequests")}</EmptyRow>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <ConfirmDialog
        open={Boolean(pendingRotate)}
        title={t("keys.reissueTitle", { keyId: pendingRotate?.keyId ?? "" })}
        confirmLabel={t("keys.reissue")}
        pendingLabel={t("keys.reissuing")}
        tone="accent"
        pending={rotateKey.isPending}
        onCancel={() => setPendingRotate(null)}
        onConfirm={() => {
          if (!pendingRotate) return;
          rotateKey.mutate(pendingRotate.hash, {
            onSuccess: (data) => {
              setPendingRotate(null);
              setIssuedKey({
                title: t("keys.reissuedTitle"),
                keyId: data.key_id,
                secret: data.key,
              });
              rotateKey.reset(); // keep the one-time secret out of React Query's cache
            },
          });
        }}
      >
        <p>{t("keys.reissueWarning")}</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title={t("keys.deleteTitle", { keyId: pendingDelete?.keyId ?? "" })}
        pending={deleteKey.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (!pendingDelete) return;
          deleteKey.mutate(pendingDelete.hash, {
            onSuccess: () => setPendingDelete(null),
          });
        }}
      >
        <p>{t("keys.deleteWarning")}</p>
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
