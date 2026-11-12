import { useCallback, useEffect, useState } from 'react';
import { ApiError, type Escalation, isEscalation, OPERATOR_PASSCODE_HEADER } from '../lib/api.ts';
import { readEventStream, type SseMessage } from '../lib/sse.ts';

export type FeedStatus = 'connecting' | 'live' | 'reconnecting' | 'unauthorized' | 'disabled';

export interface EscalationFeed {
  escalations: Escalation[];
  status: FeedStatus;
  /** Apply a local change (e.g. a resolve response) before the stream echoes it. */
  upsert: (escalation: Escalation) => void;
}

const MAX_BACKOFF_MS = 15_000;

function newestFirst(items: Iterable<Escalation>): Escalation[] {
  return [...items].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/**
 * Wait before reconnecting: what a 429 asked for (an auth lockout lasts minutes, so this isn't
 * capped), otherwise exponential backoff from the server's `retry:` hint.
 */
export function reconnectDelay(error: unknown, retryMs: number | null, attempt: number): number {
  if (error instanceof ApiError && error.status === 429 && error.retryAfterMs !== null) {
    return error.retryAfterMs;
  }
  return Math.min(MAX_BACKOFF_MS, (retryMs ?? 1000) * 2 ** attempt);
}

/**
 * The escalation the console shows. While the operator is on its call, the last-known copy stays
 * up even if the list loses it (a fresh snapshot after a server restart), so the call isn't
 * unmounted out from under them.
 */
export function shownEscalation(
  escalations: readonly Escalation[],
  selectedId: string | null,
  lastKnown: Escalation | null,
  inCall: boolean,
): Escalation | null {
  if (selectedId === null) return null;
  const listed = escalations.find((e) => e.id === selectedId);
  if (listed) return listed;
  return inCall && lastKnown?.id === selectedId ? lastKnown : null;
}

function parse(message: SseMessage): unknown {
  try {
    return JSON.parse(message.data);
  } catch {
    return null;
  }
}

/** Live escalation list from `GET /api/escalations/stream`, reconnecting with backoff. */
export function useEscalationFeed(passcode: string): EscalationFeed {
  const [items, setItems] = useState<Map<string, Escalation>>(() => new Map());
  const [status, setStatus] = useState<FeedStatus>('connecting');

  const upsert = useCallback((escalation: Escalation) => {
    setItems((prev) => new Map(prev).set(escalation.id, escalation));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let attempt = 0;
    let timer: number | undefined;

    const onMessage = (message: SseMessage) => {
      const body = parse(message);
      if (message.event === 'snapshot' && typeof body === 'object' && body !== null) {
        const list = (body as { escalations?: unknown }).escalations;
        if (Array.isArray(list)) {
          setItems(new Map(list.filter(isEscalation).map((e) => [e.id, e])));
        }
      } else if (message.event === 'escalation' && typeof body === 'object' && body !== null) {
        const escalation = (body as { escalation?: unknown }).escalation;
        if (isEscalation(escalation)) upsert(escalation);
      }
    };

    const connect = async () => {
      let retryMs: number | null = null;
      let failure: unknown = null;
      try {
        retryMs = await readEventStream('/api/escalations/stream', {
          headers: { [OPERATOR_PASSCODE_HEADER]: passcode },
          signal: controller.signal,
          idleTimeoutMs: 45_000, // the server sends a keepalive every 15 s
          onOpen: () => {
            attempt = 0;
            setStatus('live');
          },
          onMessage,
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 503)) {
          // Retrying won't help: the passcode is wrong, or the console is switched off.
          setStatus(error.status === 401 ? 'unauthorized' : 'disabled');
          return;
        }
        failure = error;
      }
      if (controller.signal.aborted) return;
      setStatus('reconnecting');
      const delay = reconnectDelay(failure, retryMs, attempt);
      attempt += 1;
      timer = window.setTimeout(() => void connect(), delay);
    };

    void connect();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [passcode, upsert]);

  return { escalations: newestFirst(items.values()), status, upsert };
}
