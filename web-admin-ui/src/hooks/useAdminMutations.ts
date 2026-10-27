import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError } from "../api/client";
import type { ApiMessage, KeyAddPayload, KeyIssued } from "../api/types";
import { useToast } from "../components/Toast";
import { localizeApiMessage } from "../i18n";

function useMutationToast<TVariables, TData>(
  mutationFn: (variables: TVariables) => Promise<TData>,
  invalidate: "proxies" | "keys",
  successMessage?: (data: TData) => string,
) {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<TData, ApiError, TVariables>({
    mutationFn,
    onSuccess: (data) => {
      const message = successMessage ? successMessage(data) : "";
      if (message) toast.push(localizeApiMessage(message));
      void queryClient.invalidateQueries({ queryKey: [invalidate] });
    },
    onError: (error) => {
      toast.push(error.message, "error");
    },
  });
}

export function useRefreshProxies() {
  return useMutationToast<void, ApiMessage>(
    () => api.refreshProxies(),
    "proxies",
    (data) => data.message,
  );
}

export function useSetProxy() {
  return useMutationToast<{ proxyId: string; enabled: boolean }, ApiMessage>(
    ({ proxyId, enabled }) => api.setProxy(proxyId, enabled),
    "proxies",
    (data) => data.message,
  );
}

export function useAddKey() {
  return useMutationToast<KeyAddPayload, KeyIssued>(
    (payload) => api.addKey(payload),
    "keys",
    (data) => data.message,
  );
}

export function useRotateKey() {
  return useMutationToast<string, KeyIssued>(
    (keyHash) => api.rotateKey(keyHash),
    "keys",
    (data) => data.message,
  );
}

export function useSetKey() {
  return useMutationToast<{ keyHash: string; enabled: boolean }, ApiMessage>(
    ({ keyHash, enabled }) => api.setKey(keyHash, enabled),
    "keys",
    (data) => data.message,
  );
}

export function useDeleteKey() {
  return useMutationToast<string, ApiMessage>(
    (keyHash) => api.deleteKey(keyHash),
    "keys",
    (data) => data.message,
  );
}
