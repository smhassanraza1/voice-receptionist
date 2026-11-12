import {
  RoomAudioRenderer,
  SessionProvider,
  useAgent,
  useAudioPlayback,
  useIsSpeaking,
  useLocalParticipant,
  useRemoteParticipants,
  useSession,
  useSessionContext,
} from '@livekit/components-react';
import { ConnectionState, TokenSource } from 'livekit-client';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AgentVisualizer } from '../components/AgentVisualizer.tsx';
import { CallControls } from '../components/CallControls.tsx';
import { CallSetup } from '../components/CallSetup.tsx';
import { Callout } from '../components/Callout.tsx';
import { HandoffBanner } from '../components/HandoffBanner.tsx';
import { PhoneIcon, SpeakerIcon, Spinner } from '../components/Icon.tsx';
import { SiteFooter, SiteHeader } from '../components/SiteChrome.tsx';
import { TelemetryPanel } from '../components/telemetry/TelemetryPanel.tsx';
import { Transcript } from '../components/Transcript.tsx';
import { useCallLifecycle } from '../hooks/useCallLifecycle.ts';
import { useCallTranscript } from '../hooks/useCallTranscript.ts';
import { useElapsed } from '../hooks/useElapsed.ts';
import { useReceptionistTelemetry } from '../hooks/useReceptionistTelemetry.ts';
import { fetchCallerToken } from '../lib/api.ts';
import { liveView } from '../lib/backstage.ts';
import {
  type CallConfig,
  coerceCallConfig,
  describeConfig,
  LANGUAGES,
  loadCallConfig,
  saveCallConfig,
} from '../lib/config.ts';
import { formatDuration } from '../lib/format.ts';
import { describeStartError } from '../lib/startErrors.ts';
import { OPERATOR_PREFIX } from '../lib/transcript.ts';

const AGENT_CONNECT_TIMEOUT_MS = 25_000;

function parseJson(text: string | undefined): unknown {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

/**
 * Owns the LiveKit session. The call options travel as `agentMetadata` in the token-source
 * options, which is also exactly what the server embeds as the agent's dispatch metadata, and
 * keys the SDK's token cache (a changed option fetches a fresh token and room).
 */
export function CallerPage() {
  const [config, setConfig] = useState<CallConfig>(loadCallConfig);
  const [callKey, setCallKey] = useState(0);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(() => {
    document.title = 'Front desk · Lakeside Family Clinic';
  }, []);

  const tokenSource = useMemo(
    () =>
      TokenSource.custom(async (options) => {
        const details = await fetchCallerToken(coerceCallConfig(parseJson(options.agentMetadata)));
        return { serverUrl: details.serverUrl, participantToken: details.participantToken };
      }),
    [],
  );
  const session = useSession(tokenSource, {
    agentMetadata: JSON.stringify(config),
    agentConnectTimeoutMilliseconds: AGENT_CONNECT_TIMEOUT_MS,
  });
  const { start, end } = session;

  const startCall = useCallback(async () => {
    saveCallConfig(config);
    setStartError(null);
    setStarting(true);
    setCallKey((key) => key + 1); // fresh transcript and telemetry for every call
    try {
      // Click handler, so the browser lets the page start audio playback.
      await start();
    } catch (error) {
      setStartError(describeStartError(error));
      await end().catch(() => undefined);
    } finally {
      setStarting(false);
    }
  }, [config, start, end]);

  return (
    <SessionProvider session={session}>
      <RoomAudioRenderer />
      <div className="app">
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <SiteHeader current="front-desk" />
        <CallWorkspace
          key={callKey}
          config={config}
          onConfigChange={setConfig}
          onStart={() => void startCall()}
          starting={starting}
          startError={startError}
          hasCalled={callKey > 0}
        />
        <SiteFooter />
      </div>
    </SessionProvider>
  );
}

interface CallWorkspaceProps {
  config: CallConfig;
  onConfigChange: (config: CallConfig) => void;
  onStart: () => void;
  starting: boolean;
  startError: string | null;
  hasCalled: boolean;
}

type Phase = 'idle' | 'connecting' | 'live' | 'ended';

/** Everything for one call. Remounted per call (React key), so state never leaks between calls. */
function CallWorkspace({ config, onConfigChange, onStart, starting, startError, hasCalled }: CallWorkspaceProps) {
  const session = useSessionContext();
  const agent = useAgent();
  const telemetry = useReceptionistTelemetry();
  const transcript = useCallTranscript();
  const staff = useRemoteParticipants().filter((p) => p.identity.startsWith(OPERATOR_PREFIX));
  const { canPlayAudio, startAudio } = useAudioPlayback();
  const callerSpeaking = useIsSpeaking(useLocalParticipant().localParticipant);
  const { connectedAt, endedAt } = useCallLifecycle(session);
  const elapsed = useElapsed(connectedAt, endedAt);
  const [ending, setEnding] = useState(false);

  const phase: Phase = session.isConnected
    ? 'live'
    : starting || session.connectionState === ConnectionState.Connecting
      ? 'connecting'
      : connectedAt !== null
        ? 'ended'
        : 'idle';

  const hangUp = async () => {
    setEnding(true);
    try {
      await session.end();
    } finally {
      setEnding(false);
    }
  };

  const view =
    phase === 'live' || phase === 'connecting'
      ? liveView({
          agentState: agent.state,
          callerSpeaking,
          transcript,
          tools: telemetry.tools,
          handoffs: telemetry.handoffs,
          staffOnLine: staff.length > 0,
        })
      : null;

  const announcement =
    phase === 'connecting' ? 'Calling the front desk' : phase === 'live' ? 'Call connected' : phase === 'ended' ? 'Call ended' : '';

  return (
    <main id="main" className="caller-layout" data-phase={phase}>
      <output className="visually-hidden">{announcement}</output>
      <div className="caller-main">
        {phase === 'idle' || phase === 'ended' ? (
          <IntroCard
            config={config}
            onConfigChange={onConfigChange}
            onStart={onStart}
            starting={starting}
            startError={startError}
            endedAfter={phase === 'ended' ? elapsed : null}
          />
        ) : (
          <>
            <HandoffBanner handoffs={telemetry.handoffs} staff={staff} />
            {agent.state === 'failed' ? (
              <Callout tone="error" title="The receptionist didn’t pick up">
                {agent.failureReasons.join(' ') ||
                  'No agent joined the call. Check that the agent worker is running.'}
              </Callout>
            ) : null}
            {phase === 'live' && !canPlayAudio ? (
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
            <section className="card console" aria-label="Call">
              <div className="console__top">
                {phase === 'live' ? (
                  <span className="live-pill">
                    <span className="live-pill__dot" aria-hidden="true" /> Live
                  </span>
                ) : (
                  <span className="live-pill live-pill--pending">
                    <Spinner size={12} /> Calling
                  </span>
                )}
                <span className="console__timer" aria-label="Call duration">
                  {formatDuration(elapsed)}
                </span>
              </div>
              <AgentVisualizer
                state={agent.state}
                track={agent.microphoneTrack}
                label={staff.length > 0 ? 'Staff member on the line' : undefined}
              />
              <p className="console__config">
                {describeConfig(config, telemetry.session)} ·{' '}
                {LANGUAGES.find((l) => l.value === config.language)?.label ?? config.language}
              </p>
              <CallControls
                micTrack={session.isConnected ? session.local.microphoneTrack : undefined}
                onEnd={() => void hangUp()}
                ending={ending}
              />
            </section>
          </>
        )}
        {phase !== 'idle' || transcript.length > 0 ? (
          <Transcript
            entries={transcript}
            emptyHint={
              phase === 'connecting'
                ? 'Connecting… you can start talking as soon as the line opens.'
                : 'The conversation appears here, both sides, as it happens.'
            }
          />
        ) : (
          <TryThis />
        )}
      </div>
      <TelemetryPanel telemetry={telemetry} requested={config} view={view} hasCalled={hasCalled} />
    </main>
  );
}

interface IntroCardProps {
  config: CallConfig;
  onConfigChange: (config: CallConfig) => void;
  onStart: () => void;
  starting: boolean;
  startError: string | null;
  endedAfter: number | null;
}

function IntroCard({ config, onConfigChange, onStart, starting, startError, endedAfter }: IntroCardProps) {
  return (
    <section className="card intro" aria-labelledby="intro-title">
      {endedAfter !== null ? (
        <p className="intro__ended">
          <span className="badge">Call ended</span> {formatDuration(endedAfter)} on the line. The
          transcript and telemetry are below.
        </p>
      ) : (
        <p className="eyebrow">Front desk · AI receptionist</p>
      )}
      <h1 id="intro-title" className="display">
        Call the <em>front desk</em>.
      </h1>
      <p className="lede">
        Book, move or cancel an appointment, ask about a bill or an insurance claim, or ask for a
        person. You’ll be talking to an AI voice agent; the clinic and its patients are made up.
      </p>
      <CallSetup config={config} onChange={onConfigChange} disabled={starting} />
      {startError ? (
        <Callout tone="error" title="The call didn’t connect">
          {startError}
        </Callout>
      ) : null}
      <div className="intro__cta">
        <button type="button" className="btn btn--primary btn--lg" onClick={onStart} disabled={starting}>
          {starting ? <Spinner /> : <PhoneIcon />}
          {endedAfter !== null ? 'Call again' : 'Call the clinic'}
        </button>
        <p className="intro__fine">Uses your microphone. Headphones keep the agent from hearing itself.</p>
      </div>
    </section>
  );
}

/** Seeded demo data the MCP server knows about, so a first call has something to do. */
function TryThis() {
  return (
    <section className="card try" aria-labelledby="try-title">
      <h2 id="try-title" className="eyebrow">
        Things to try
      </h2>
      <ul className="try__list">
        <li>“I need a pediatrics appointment for my son on Saturday morning.”</li>
        <li>“Do you take Northwind HMO?”</li>
        <li>“¿Hay algún médico que hable español?”</li>
        <li>“I owe something on my last bill. Can you check?”</li>
        <li>“Can I talk to a person, please?”</li>
      </ul>
      <div className="patient-card" aria-label="Demo patient">
        <p className="patient-card__label">Demo patient</p>
        <dl>
          <div>
            <dt>Name</dt>
            <dd>James Whitfield</dd>
          </div>
          <div>
            <dt>Date of birth</dt>
            <dd>Nov 2, 1972</dd>
          </div>
          <div>
            <dt>Confirmation</dt>
            <dd>
              <code>LKC-4H8T</code>
            </dd>
          </div>
          <div>
            <dt>Phone</dt>
            <dd>
              <code>+1 555 010 1002</code>
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}
