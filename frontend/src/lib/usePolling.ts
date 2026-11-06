import { useEffect, useRef } from "react";

/**
 * Run `fn` on an interval while `enabled`, with correct lifecycle:
 *  - always tears down the interval on unmount or when `enabled` flips false;
 *  - skips ticks while the browser tab is hidden (no pointless polling);
 *  - reads the latest `fn` each tick via a ref, so callers can pass an inline
 *    closure without restarting the interval or capturing stale state.
 *
 * Replaces the ~15 hand-rolled setInterval blocks across the app, each of which
 * re-implemented start/cleanup slightly differently (and one of which leaked).
 * Does NOT fire immediately — do the initial load yourself so loading/error
 * state stays explicit at the call site.
 */
export function usePolling(
  fn: () => void | Promise<void>,
  ms: number,
  enabled: boolean = true,
): void {
  const saved = useRef(fn);
  saved.current = fn;

  useEffect(() => {
    if (!enabled || ms <= 0) return;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void saved.current();
    };
    const id = setInterval(tick, ms);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [ms, enabled]);
}
