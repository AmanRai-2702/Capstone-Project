import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> { data: T | null; error: string | null; loading: boolean; reload: () => void }

/** Runs `fn` when `deps` change; ignores results of superseded calls (no stale data after a quick navigation). */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], enabled = true): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [tick, setTick] = useState(0);
  const latest = useRef(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) { setLoading(false); return; }
    const mine = ++latest.current;
    setLoading(true);
    setError(null);
    fnRef.current().then(
      (d) => { if (mine === latest.current) { setData(d); setLoading(false); } },
      (e: unknown) => { if (mine === latest.current) { setError(e instanceof Error ? e.message : String(e)); setLoading(false); } },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}
