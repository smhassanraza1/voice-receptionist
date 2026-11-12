import { type AgentState, BarVisualizer, type TrackReference } from '@livekit/components-react';

const STATE_LABELS: Record<AgentState, string> = {
  disconnected: 'Not connected',
  connecting: 'Connecting…',
  'pre-connect-buffering': 'Go ahead, it’s listening',
  initializing: 'Picking up…',
  idle: 'On the line',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  failed: 'Couldn’t connect',
};

function agentStateLabel(state: AgentState): string {
  return STATE_LABELS[state];
}

interface AgentVisualizerProps {
  state: AgentState;
  track?: TrackReference;
  /** Override the state label, e.g. when a staff member has taken over. */
  label?: string;
}

/**
 * The receptionist's voice as seven bars. BarVisualizer drives heights from the audio track and
 * sequences highlighted bars for connecting/thinking; the styling lives in styles/caller.css.
 */
export function AgentVisualizer({ state, track, label }: AgentVisualizerProps) {
  const text = label ?? agentStateLabel(state);
  return (
    <figure className="agent-visual" data-state={state}>
      <div className="agent-visual__well">
        <BarVisualizer
          state={state}
          track={track}
          barCount={7}
          options={{ minHeight: 12, maxHeight: 100 }}
          className="agent-bars"
          aria-hidden="true"
        />
      </div>
      <figcaption className="agent-visual__caption">
        <span className="agent-visual__name">Receptionist</span>
        {/* Not a live region: states flip every few seconds and would talk over the call. */}
        <span className="agent-visual__state">{text}</span>
      </figcaption>
    </figure>
  );
}
