import { SessionEvent, type UseSessionReturn, useEvents } from '@livekit/components-react';
import { ConnectionState } from 'livekit-client';
import { useState } from 'react';

export interface CallLifecycle {
  connectedAt: number | null;
  endedAt: number | null;
}

const NOT_STARTED: CallLifecycle = { connectedAt: null, endedAt: null };

/**
 * When this call connected and when it ended (ms epochs), recorded from the session's
 * connection-state events. Mount a fresh consumer per call to reset it.
 */
export function useCallLifecycle(session: UseSessionReturn): CallLifecycle {
  const [lifecycle, setLifecycle] = useState<CallLifecycle>(NOT_STARTED);
  useEvents(
    session,
    SessionEvent.ConnectionStateChanged,
    (state: ConnectionState) => {
      const at = Date.now();
      if (state === ConnectionState.Connected) {
        setLifecycle((prev) => (prev.connectedAt === null ? { connectedAt: at, endedAt: null } : prev));
      } else if (state === ConnectionState.Disconnected) {
        setLifecycle((prev) =>
          prev.connectedAt !== null && prev.endedAt === null ? { ...prev, endedAt: at } : prev,
        );
      }
    },
    [],
  );
  return lifecycle;
}
