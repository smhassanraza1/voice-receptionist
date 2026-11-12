import { type TrackReference, useTrackToggle, useTrackVolume } from '@livekit/components-react';
import { Track } from 'livekit-client';
import type { CSSProperties } from 'react';
import { HangUpIcon, MicIcon, MicOffIcon, Spinner } from './Icon.tsx';

interface CallControlsProps {
  micTrack?: TrackReference;
  onEnd: () => void;
  ending?: boolean;
  endLabel?: string;
}

/** Mute toggle with a live input meter, and the hang-up button. */
export function CallControls({ micTrack, onEnd, ending = false, endLabel = 'End call' }: CallControlsProps) {
  const { toggle, enabled, pending } = useTrackToggle({ source: Track.Source.Microphone });
  const level = useTrackVolume(micTrack);
  // Until the mic is published (still connecting) there's nothing to mute yet.
  const micLive = micTrack !== undefined;
  const muted = micLive && !enabled;

  return (
    <div className="call-controls">
      <button
        type="button"
        className="btn btn--control"
        aria-pressed={muted}
        disabled={pending || !micLive}
        onClick={() => void toggle()}
        style={{ '--level': muted ? 0 : Math.min(1, level * 2.5) } as CSSProperties}
      >
        {muted ? <MicOffIcon /> : <MicIcon />}
        <span>Mute</span>
        <span className="mic-meter" aria-hidden="true" />
      </button>
      <button type="button" className="btn btn--danger" onClick={onEnd} disabled={ending}>
        {ending ? <Spinner /> : <HangUpIcon />}
        <span>{endLabel}</span>
      </button>
    </div>
  );
}
