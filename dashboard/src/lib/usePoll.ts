import { useCallback, useEffect, useRef, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
}

/**
 * Calls `fn` immediately and then every `intervalMs`. Pass `stop(data)` to end polling at a terminal state.
 * The latest `fn` is always used; in-flight requests are ignored after unmount.
 */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number, stop?: (data: T) => boolean, deps: unknown[] = []): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const stopRef = useRef(stop);
  stopRef.current = stop;
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async () => {
      try {
        const d = await fnRef.current();
        if (!alive) return;
        setData(d);
        setError(null);
        setLoading(false);
        if (stopRef.current?.(d)) return;
      } catch (e) {
        if (!alive) return;
        setError(e as Error);
        setLoading(false);
      }
      if (alive && intervalMs > 0) timer = setTimeout(run, intervalMs);
    };
    setLoading(true);
    void run();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, tick, ...deps]);

  return { data, error, loading, refresh };
}
