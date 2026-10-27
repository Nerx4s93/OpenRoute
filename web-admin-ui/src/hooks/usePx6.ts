import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ApiError } from "../api/client";
import type {
  ProxyBuyPayload,
  ProxyBuyResult,
  Px6Account,
  Px6Countries,
  Px6Price,
} from "../api/types";
import { useToast } from "../components/Toast";
import { localizeApiMessage } from "../i18n";

export function usePx6Account() {
  return useQuery<Px6Account, ApiError>({
    queryKey: ["px6", "account"],
    queryFn: api.px6Account,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function usePx6Countries(version: number, enabled = false) {
  return useQuery<Px6Countries, ApiError>({
    queryKey: ["px6", "countries", version],
    queryFn: () => api.px6Countries(version),
    enabled,
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function usePx6Price(
  version: number,
  count: number,
  period: number,
  enabled = false,
) {
  return useQuery<Px6Price, ApiError>({
    queryKey: ["px6", "price", version, count, period],
    queryFn: () => api.px6Price(version, count, period),
    enabled: enabled && count > 0,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useBuyProxies() {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<ProxyBuyResult, ApiError, ProxyBuyPayload>({
    mutationFn: api.buyProxies,
    onSuccess: (data) => {
      toast.push(localizeApiMessage(data.message));
      // Keep the balance card in sync without issuing another px6.net request.
      queryClient.setQueryData<Px6Account>(["px6", "account"], (current) => ({
        user_id: current?.user_id ?? "",
        balance: data.balance,
        currency: data.currency || current?.currency || "RUB",
      }));
      void queryClient.invalidateQueries({ queryKey: ["proxies"] });
    },
    onError: (error) => {
      toast.push(error.message, "error");
    },
  });
}
