import {
  BarVisualizer,
  RoomAudioRenderer,
  SessionProvider,
  type TrackReference,
  useAudioPlayback,
  useIsSpeaking,
  useRemoteParticipants,
  useSession,
  useSessionContext,
  useTracks,
} from '@livekit/components-react';
import { type Participant, ParticipantKind, type RemoteParticipant, TokenSource, Track } from 'livekit-client';
import { useEffect, useMemo, useState } from 'react';
import { CallControls } from '../components/CallControls.tsx';
import { Callout } from '../components/Callout.tsx';
import { CheckIcon, HeadsetIcon, SpeakerIcon, Spinner, XIcon } from '../components/Icon.tsx';
import { Transcript } from '../components/Transcript.tsx';
import { useCallLifecycle } from '../hooks/useCallLifecycle.ts';
import { useCallTranscript } from '../hooks/useCallTranscript.ts';
import { useElapsed, useNow } from '../hooks/useElapsed.ts';
import { type Escalation, errorMessage, fetchOperatorToken, resolveEscalation } from '../lib/api.ts';
import { formatClock, formatDuration, formatRelative } from '../lib/format.ts';
import { OPERATOR_PREFIX } from '../lib/transcript.ts';
import type { OperatorAuth } from './OperatorPage.tsx';

interface EscalationDetailProps {
  escalation: Escalation;
  auth: OperatorAuth;
  onUpdated: (escalation: Escalation) => void;
  onInCallChange: (inCall: boolean) => void;
  onClose: () => void;
}

/**
 * One escalation, and the LiveKit session for joining its room. Mounted per escalation (React
 * key), so the SDK's connection warm-up fetches an operator token for this room only.
 */
export function EscalationDetail({ escalation, auth, onUpdated, onInCallChange, onClose }: EscalationDetailProps) {
  const tokenSource = useMemo(
    () =>
      TokenSource.custom(async (options) => {
        const details = await fetchOperatorToken({
          room: options.roomName ?? escalation.room,
          name: options.participantName ?? auth.name,
          passcode: auth.passcode,
        });
        return { serverUrl: details.serverUrl, participantToken: details.participantToken };
      }),
    [escalation.room, auth.name, auth.passcode],
  );
  const session = useSession(tokenSource, { roomName: escalation.room, participantName: auth.name });

  useEffect(() => {
    onInCallChange(session.isConnected);
  }, [session.isConnected, onInCallChange]);
  useEffect(() => () => onInCallChange(false), [onInCallChange]);

  return (
    <SessionProvider session={session}>
      <RoomAudioRenderer />
      <DetailBody escalation={escalation} auth={auth} onUpdated={onUpdated} onClose={onClose} />
    </SessionProvider>
  );
}

function DetailBody({
  escalation,
  auth,
  onUpdated,
  onClose,
}: Pick<EscalationDetailProps, 'escalation' | 'auth' | 'onUpdated' | 'onClose'>) {
  const session = useSessionContext();
  const transcript = useCallTranscript();
  const { connectedAt, endedAt } = useCallLifecycle(session);
  const elapsed = useElapsed(connectedAt, endedAt);
  const now = useNow(15_000);
  const [joining, setJoining] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const join = async () => {
    setError(null);
    setJoining(true);
    try {
      // Plain mic: the pre-connect buffer is for agents, and the agent is already here.
      await session.start({ tracks: { microphone: { enabled: true } } });
    } catch (err) {
      setError(errorMessage(err, 'Couldn’t join the call.'));
      await session.end().catch(() => undefined);
    } finally {
      setJoining(false);
    }
  };

  const leave = async () => {
    setLeaving(true);
    try {
      await session.end();
    } finally {
      setLeaving(false);
    }
  };

  const resolve = async () => {
    setError(null);
    setResolving(true);
    try {
      onUpdated(await resolveEscalation(escalation.id, auth.passcode));
    } catch (err) {
      setError(errorMessage(err, 'Couldn’t mark it resolved.'));
    } finally {
      setResolving(false);
    }
  };

  const resolved = escalation.status === 'resolved';

  return (
    <section className="card detail" aria-labelledby="detail-title">
      <header className="detail__header">
        <div>
          <p className="eyebrow">
            Escalation · {resolved ? 'resolved' : 'open'}
            <span className="eyebrow__aside">opened {formatRelative(escalation.created_at, now)}</span>
          </p>
          <h2 id="detail-title" className="display display--sm">
            {escalation.reason}
          </h2>
          <p className="detail__meta">
            <code>{escalation.room}</code>
            <span aria-hidden="true">·</span>
            <time dateTime={escalation.created_at}>{formatClock(escalation.created_at)}</time>
          </p>
        </div>
        <button
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onClose}
          disabled={session.isConnected}
          aria-label="Close escalation"
        >
          <XIcon />
        </button>
      </header>

      <div className="detail__summary">
        <h3 className="eyebrow">Receptionist’s summary</h3>
        <p>{escalation.summary || 'No summary was provided.'}</p>
      </div>

      {error ? (
        <Callout tone="error" title="Something went wrong">
          {error}
        </Callout>
      ) : null}

      {session.isConnected ? (
        <InCall elapsed={elapsed} onLeave={() => void leave()} leaving={leaving} />
      ) : null}

      <div className="detail__actions">
        {session.isConnected ? null : (
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => void join()}
            disabled={joining || resolved}
            title={resolved ? 'This escalation is resolved' : undefined}
          >
            {joining ? <Spinner /> : <HeadsetIcon />} {connectedAt ? 'Rejoin call' : 'Join call'}
          </button>
        )}
        <button
          type="button"
          className="btn btn--secondary"
          onClick={() => void resolve()}
          disabled={resolving || resolved}
        >
          {resolving ? <Spinner /> : <CheckIcon />} {resolved ? 'Resolved' : 'Mark resolved'}
        </button>
      </div>

      {connectedAt !== null || transcript.length > 0 ? (
        <Transcript
          entries={transcript}
          title="Live transcript"
          emptyHint="Lines spoken from now on appear here. Earlier conversation is in the summary above."
        />
      ) : null}
    </section>
  );
}

function roleOf(p: Participant): { label: string; kind: 'caller' | 'agent' | 'staff' } {
  if (p.kind === ParticipantKind.AGENT) return { label: 'Receptionist (AI, muted)', kind: 'agent' };
  if (p.identity.startsWith(OPERATOR_PREFIX)) return { label: p.name || 'Staff', kind: 'staff' };
  return { label: p.kind === ParticipantKind.SIP ? 'Caller (phone)' : 'Caller', kind: 'caller' };
}

function ParticipantRow({ participant, track }: { participant: RemoteParticipant; track?: TrackReference }) {
  const speaking = useIsSpeaking(participant);
  const { label, kind } = roleOf(participant);
  return (
    <li className={`participant participant--${kind}`} data-speaking={speaking || undefined}>
      <span className="participant__name">
        {label}
        {speaking ? <span className="visually-hidden"> (speaking)</span> : null}
      </span>
      <BarVisualizer track={track} barCount={5} className="mini-bars" aria-hidden="true" />
    </li>
  );
}

function InCall({ elapsed, onLeave, leaving }: { elapsed: number; onLeave: () => void; leaving: boolean }) {
  const session = useSessionContext();
  const remote = useRemoteParticipants();
  const micTracks = useTracks([Track.Source.Microphone]);
  const { canPlayAudio, startAudio } = useAudioPlayback();
  const trackFor = (identity: string) => micTracks.find((t) => t.participant.identity === identity);
  const callerPresent = remote.some((p) => roleOf(p).kind === 'caller');

  return (
    <div className="operator-call">
      <div className="console__top">
        <span className="live-pill">
          <span className="live-pill__dot" aria-hidden="true" /> On the call
        </span>
        <span className="console__timer" aria-label="Time on call">
          {formatDuration(elapsed)}
        </span>
      </div>
      {!callerPresent ? (
        <Callout tone="info" title="The caller isn’t in the room">
          They may have hung up. You can leave and mark this escalation resolved.
        </Callout>
      ) : null}
      {!canPlayAudio ? (
        <Callout
          tone="info"
          title="Your browser paused call audio"
          action={
            <button type="button" className="btn btn--secondary" onClick={() => void startAudio()}>
              <SpeakerIcon /> Turn on audio
            </button>
          }
        />
      ) : null}
      <ul className="participants" aria-label="In this call">
        {remote.map((p) => (
          <ParticipantRow key={p.identity} participant={p} track={trackFor(p.identity)} />
        ))}
      </ul>
      <CallControls
        micTrack={session.isConnected ? session.local.microphoneTrack : undefined}
        onEnd={onLeave}
        ending={leaving}
        endLabel="Leave call"
      />
    </div>
  );
}
