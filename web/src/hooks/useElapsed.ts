import { useEffect, useState } from 'react';

/**
 * Seconds from `since` to `until` (or to now, ticking each second while `until` is null).
 * Both are ms epochs; returns 0 before `since` is known.
 */
export function useElapsed(since: number | null, until: number | null = null): number {
  const [now, setNow] = useState(() => Date.now());
  const ticking = since !== null && until === null;
  useEffect(() => {
    if (!ticking) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [ticking]);
  if (since === null) return 0;
  return Math.max(0, ((until ?? Math.max(now, since)) - since) / 1000);
}

/** Re-render every `intervalMs` (for relative timestamps like "3 min ago"). */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
