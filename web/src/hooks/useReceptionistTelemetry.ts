import { useDataChannel } from '@livekit/components-react';
import { type Participant, ParticipantKind } from 'livekit-client';
import { useCallback, useReducer } from 'react';
import {
  initialTelemetry,
  parseReceptionistEvent,
  RECEPTIONIST_EVENTS_TOPIC,
  telemetryReducer,
  type Telemetry,
} from '../lib/events.ts';

interface DataMessage {
  payload: Uint8Array;
  from?: Participant;
}

/**
 * Collects the agent's `receptionist.events` into one telemetry state. Must render inside a
 * `SessionProvider`. Events from non-agent participants are ignored, so another person in the
 * room can't spoof metrics or handoff banners.
 */
export function useReceptionistTelemetry(): Telemetry {
  const [telemetry, dispatch] = useReducer(telemetryReducer, undefined, initialTelemetry);

  const onMessage = useCallback((message: DataMessage) => {
    if (message.from && message.from.kind !== ParticipantKind.AGENT) return;
    const event = parseReceptionistEvent(message.payload);
    if (event) dispatch({ type: 'event', event, receivedAt: Date.now() });
  }, []);

  useDataChannel(RECEPTIONIST_EVENTS_TOPIC, onMessage);
  return telemetry;
}
