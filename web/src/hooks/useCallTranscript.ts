import { useSessionMessages } from '@livekit/components-react';
import { useMemo, useState } from 'react';
import { type TranscriptEntry, toTranscript } from '../lib/transcript.ts';

/**
 * The live transcript, kept after hang-up. The SDK clears its message list when the room
 * disconnects; holding the last non-empty list lets people review the call afterwards. Remount
 * the consumer (a new React key per call) to start fresh.
 */
export function useCallTranscript(): TranscriptEntry[] {
  const { messages } = useSessionMessages();
  const entries = useMemo(() => toTranscript(messages), [messages]);
  const [retained, setRetained] = useState(entries);
  if (entries.length > 0 && entries !== retained) setRetained(entries);
  return entries.length > 0 ? entries : retained;
}
