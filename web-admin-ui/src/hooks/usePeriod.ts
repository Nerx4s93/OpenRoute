import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { isPeriod, type Period } from "../api/types";

// Browser-local preference: survives navigation, reloads and new browser tabs.
// The URL stays the source of truth when it contains an explicit period.
const STORAGE_KEY = "openroute.admin.period";

function readStoredPeriod(): Period | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return isPeriod(raw) ? raw : null;
  } catch {
    return null;
  }
}

function storePeriod(period: Period): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, period);
  } catch {
    // Storage can be unavailable (private mode / disabled); keep it in memory.
  }
}

export function usePeriod(): [Period, (period: Period) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("period");
  const fromUrl = isPeriod(raw) ? raw : null;
  const [stored, setStored] = useState<Period | null>(readStoredPeriod);
  const period: Period = fromUrl ?? stored ?? "all";

  useEffect(() => {
    if (!fromUrl) return;
    setStored(fromUrl);
    storePeriod(fromUrl);
  }, [fromUrl]);

  const setPeriod = useCallback(
    (next: Period) => {
      setStored(next);
      storePeriod(next);
      const updated = new URLSearchParams(params);
      if (next === "all") updated.delete("period");
      else updated.set("period", next);
      setParams(updated, { replace: true });
    },
    [params, setParams],
  );

  return [period, setPeriod];
}
