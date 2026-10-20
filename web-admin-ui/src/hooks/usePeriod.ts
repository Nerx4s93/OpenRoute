import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { isPeriod, type Period } from "../api/types";

export function usePeriod(): [Period, (period: Period) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("period");
  const period: Period = isPeriod(raw) ? raw : "all";

  const setPeriod = useCallback(
    (next: Period) => {
      const updated = new URLSearchParams(params);
      if (next === "all") updated.delete("period");
      else updated.set("period", next);
      setParams(updated, { replace: true });
    },
    [params, setParams],
  );

  return [period, setPeriod];
}
