import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

interface LiveStatus {
  serverTime: string | null;
  connected: boolean;
}

interface LiveStatusContextValue extends LiveStatus {
  publish: (status: LiveStatus) => void;
}

const LiveStatusContext = createContext<LiveStatusContextValue | null>(null);

export function LiveStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<LiveStatus>({ serverTime: null, connected: false });

  const publish = useCallback((next: LiveStatus) => {
    setStatus((prev) =>
      prev.serverTime === next.serverTime && prev.connected === next.connected ? prev : next,
    );
  }, []);

  const value = useMemo<LiveStatusContextValue>(
    () => ({ ...status, publish }),
    [status, publish],
  );

  return <LiveStatusContext.Provider value={value}>{children}</LiveStatusContext.Provider>;
}

export function useLiveStatus(): LiveStatusContextValue {
  const value = useContext(LiveStatusContext);
  if (!value) throw new Error("useLiveStatus must be used inside LiveStatusProvider");
  return value;
}

export function usePublishLiveStatus(serverTime: string | null, connected: boolean): void {
  const { publish } = useLiveStatus();
  useEffect(() => {
    publish({ serverTime, connected });
  }, [publish, serverTime, connected]);
}
