import type { ReceivedMessage } from '@livekit/components-react';
import { ParticipantKind } from 'livekit-client';

export type Speaker = 'you' | 'receptionist' | 'caller' | 'staff';

export interface TranscriptEntry {
  id: string;
  speaker: Speaker;
  /** Display label, e.g. "You", "Receptionist", "Dana (staff)". */
  label: string;
  text: string;
  timestamp: number;
  /** Still being transcribed (an interim STT hypothesis). */
  interim: boolean;
}

export const OPERATOR_PREFIX = 'operator-';

const FINAL_ATTRIBUTE = 'lk.transcription_final';

/** Whose words these are, from the viewer's side of the call. */
function classify(message: ReceivedMessage): { speaker: Speaker; label: string } {
  const from = message.from;
  if (from?.isLocal) return { speaker: 'you', label: 'You' };
  if (from?.identity.startsWith(OPERATOR_PREFIX)) {
    return { speaker: 'staff', label: from.name ? `${from.name} (staff)` : 'Clinic staff' };
  }
  if (from?.kind === ParticipantKind.AGENT || (!from && message.type === 'agentTranscript')) {
    return { speaker: 'receptionist', label: 'Receptionist' };
  }
  // Another person in the room: the patient, as seen from the operator console.
  return { speaker: 'caller', label: from?.kind === ParticipantKind.SIP ? 'Caller (phone)' : 'Caller' };
}

export function toTranscript(messages: readonly ReceivedMessage[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  for (const message of messages) {
    const text = message.message.trim();
    if (!text) continue;
    const { speaker, label } = classify(message);
    entries.push({
      id: message.id,
      speaker,
      label,
      text,
      timestamp: message.timestamp,
      interim: message.attributes?.[FINAL_ATTRIBUTE] === 'false',
    });
  }
  return entries;
}
