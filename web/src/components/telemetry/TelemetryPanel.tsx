import type { LiveView, Owner } from '../../lib/backstage.ts';
import type { CallConfig } from '../../lib/config.ts';
import type { Telemetry } from '../../lib/events.ts';
import { ActivityFeed } from './ActivityFeed.tsx';
import { CallFlow } from './CallFlow.tsx';
import { LatencyPanel } from './LatencyPanel.tsx';

const OWNER_LABEL: Record<Owner, string> = {
  receptionist: 'receptionist agent',
  billing: 'billing agent',
  staff: 'staff member',
};

/** The raw signals the diagram is drawn from, as LiveKit reports them. */
function Readout({ view }: { view: LiveView }) {
  const cells: [label: string, value: string][] = [
    ['Agent state', view.agentState],
    ['Your mic', view.callerSpeaking ? 'speaking' : 'quiet'],
    ['Handled by', OWNER_LABEL[view.owner]],
  ];
  return (
    <dl className="readout">
      {cells.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

interface TelemetryPanelProps {
  telemetry: Telemetry;
  requested: CallConfig;
  /** Live state while a call is connecting or on; null before the first call and after hang-up. */
  view: LiveView | null;
  hasCalled: boolean;
}

/**
 * "Behind the scenes": what the agent does with each turn, drawn as a flow that lights up live,
 * then what it did (activity) and how fast (reply time).
 */
export function TelemetryPanel({ telemetry, requested, view, hasCalled }: TelemetryPanelProps) {
  const session = telemetry.session;
  // The diagram follows the picker until the agent reports what it actually runs.
  const mode = session?.mode ?? requested.mode;
  const models = session?.mode === mode ? session.models : {};
  const showCall = hasCalled || telemetry.eventCount > 0;

  return (
    <aside className="backstage" aria-labelledby="backstage-title">
      <header className="backstage__header">
        <p className="backstage__kicker">
          <span className="backstage__light" data-on={view ? true : undefined} aria-hidden="true" />
          {view ? 'Live from this call' : showCall ? 'From your last call' : 'How it works'}
        </p>
        <h2 id="backstage-title" className="backstage__title">
          Behind the scenes
        </h2>
        <p className="backstage__lede">
          {showCall
            ? 'What the voice agent did on each turn of the call.'
            : 'Each time you speak, your words go through these steps. Start a call and watch each one light up as it happens.'}
        </p>
        {view ? <Readout view={view} /> : null}
      </header>

      <div className="backstage__body" data-split={showCall || undefined}>
        <section className="backstage__section" aria-labelledby="flow-title">
          <h3 id="flow-title" className="visually-hidden">
            How one turn of the call flows
          </h3>
          <CallFlow mode={mode} models={models} view={view} lastTurn={telemetry.turns.at(-1) ?? null} />
        </section>

        {showCall ? (
          <div className="backstage__side">
            <section className="backstage__section" aria-labelledby="activity-title">
              <h3 id="activity-title" className="eyebrow">
                What it did
              </h3>
              <ActivityFeed activity={telemetry.activity} tools={telemetry.tools} turns={telemetry.turns} />
            </section>
            <LatencyPanel turns={telemetry.turns} serverTurns={session?.models.turns === 'server VAD'} />
          </div>
        ) : null}
      </div>

    </aside>
  );
}
