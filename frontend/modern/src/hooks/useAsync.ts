import { useEffect, useState } from "react";
import type { DependencyList } from "react";

import { invalidateApiCache, type ApiState } from "../lib/api";

/**
 * Shared loader state for page-level API calls.
 * Keep refresh and cancellation behavior consistent across every console page.
 */
export function useAsync<T>(loader: () => Promise<T>, deps: DependencyList): [ApiState<T>, () => void] {
  const [state, setState] = useState<ApiState<T>>({ loading: true });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState((current) => ({ ...current, loading: true, error: undefined }));
    loader()
      .then((data) => !cancelled && setState({ data, loading: false }))
      .catch((error) => !cancelled && setState({ loading: false, error: error.message || String(error) }));
    return () => {
      cancelled = true;
    };
  }, [...deps, tick]);

  return [state, () => {
    invalidateApiCache();
    setTick((value) => value + 1);
  }];
}
