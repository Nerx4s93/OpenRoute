import { useEffect, useMemo, useState } from "react";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { api, type ApiError } from "../api/client";
import type { KeysSnapshot, Period, ProxiesSnapshot } from "../api/types";
import { usePublishLiveStatus } from "../components/LiveStatus";

type Page = "proxies" | "keys";
type Snapshot = ProxiesSnapshot | KeysSnapshot;

function useLiveSnapshot<T extends Snapshot>(
  page: Page,
  period: Period,
  fetcher: (period: Period) => Promise<T>,
): UseQueryResult<T, ApiError> & { connected: boolean } {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  // A stable tuple is important: it is a dependency of the EventSource effect.
  const queryKey = useMemo(() => [page, period] as const, [page, period]);

  const query = useQuery<T, ApiError>({
    queryKey,
    queryFn: () => fetcher(period),
    placeholderData: keepPreviousData,
    refetchInterval: connected ? false : 5000,
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    let source: EventSource | null = null;
    let stopped = false;

    const close = () => {
      source?.close();
      source = null;
      if (!stopped) setConnected(false);
    };

    const open = () => {
      if (stopped || typeof EventSource === "undefined") {
        setConnected(false);
        return;
      }
      source?.close();
      source = new EventSource(`/api/admin/${page}/events?period=${period}`);
      source.onopen = () => {
        if (!stopped) setConnected(true);
      };
      source.onmessage = (event) => {
        try {
          const snapshot = JSON.parse(event.data) as T;
          queryClient.setQueryData<T>(queryKey, snapshot);
        } catch {
          // Ignore a malformed frame; the next tick will resend a snapshot.
        }
      };
      source.onerror = () => {
        if (!stopped) setConnected(false);
      };
    };

    const onVisibilityChange = () => {
      if (document.hidden) {
        close();
      } else {
        open();
        void query.refetch();
      }
    };

    open();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stopped = true;
      close();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
    // query.refetch is stable between renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, period, queryClient, queryKey, query.refetch]);

  usePublishLiveStatus(query.data?.server_time ?? null, connected);

  return { ...query, connected };
}

export function useProxiesLive(period: Period) {
  return useLiveSnapshot<ProxiesSnapshot>("proxies", period, api.proxies);
}

export function useKeysLive(period: Period) {
  return useLiveSnapshot<KeysSnapshot>("keys", period, api.keys);
}
