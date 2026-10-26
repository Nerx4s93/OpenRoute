import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type ApiError } from "../api/client";
import type { Period, StatsSnapshot } from "../api/types";

export function useStatsPeriod(period: Period) {
  return useQuery<StatsSnapshot, ApiError>({
    queryKey: ["stats", "period", period],
    queryFn: () => api.statsPeriod(period),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
}
