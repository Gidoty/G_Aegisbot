"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorText } from "./api";

export function useApi<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    if (path === null) return;
    try {
      const result = await api<T>(path);
      if (alive.current) {
        setData(result);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(errorText(e));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    setLoading(path !== null);
    load();
    const timer = refreshMs > 0 ? setInterval(load, refreshMs) : undefined;
    return () => {
      alive.current = false;
      if (timer) clearInterval(timer);
    };
  }, [load, path, refreshMs]);

  return { data, error, loading, reload: load };
}
