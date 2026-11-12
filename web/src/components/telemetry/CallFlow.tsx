import type { ReactNode } from 'react';
import type { Branch, BranchState, LiveView, Step } from '../../lib/backstage.ts';
import { describeTool, formatCall } from '../../lib/backstage.ts';
import type { Mode } from '../../lib/config.ts';
import type { TurnEntry } from '../../lib/events.ts';
import { formatMs } from '../../lib/format.ts';

export interface FlowModels {
  stt?: string;
  llm?: string;
  tts?: string;
  turns?: string;
}

interface StageProps {
  title: string;
  text: string;
  /** The model doing this step, when known. */
  tag?: string;
  /** Last measured time for this step, already phrased: "first words in 410 ms". */
  metric?: string | null;
  active: boolean;
  /** Extra content inside the stage (the side systems). */
  children?: ReactNode;
}

function Stage({ title, text, tag, metric, active, children }: StageProps) {
  return (
    <li className="stage" data-active={active || undefined}>
      <span className="stage__node" aria-hidden="true" />
      <div className="stage__body">
        <div className="stage__head">
          <h4 className="stage__title">{title}</h4>
          {active ? <span className="stage__now">now</span> : null}
        </div>
        <p className="stage__text">{text}</p>
        {tag || metric ? (
          <p className="stage__meta">
            {tag ? <code className="stage__tag">{tag}</code> : null}
            {metric ? <span className="stage__metric">{metric}</span> : null}
          </p>
        ) : null}
        {children}
      </div>
    </li>
  );
}

const BRANCHES: readonly { id: Branch; title: string; text: string; via: string }[] = [
  { id: 'clinic', title: 'Clinic systems', text: 'Calendar, bookings and help pages', via: 'MCP' },
  { id: 'billing', title: 'Billing specialist', text: 'A second AI agent, for bills only', via: 'A2A' },
  { id: 'person', title: 'A real person', text: 'Front-desk staff take the call', via: 'Handoff' },
];

const BRANCH_STATE_TEXT: Record<BranchState, string> = {
  idle: '',
  busy: 'working',
  connected: 'on the call',
};

function SideSystems({ view }: { view: LiveView | null }) {
  const tool = view?.running ?? null;
  const running = tool ? describeTool(tool.name) : null;
  return (
    <div className="branches">
      <p className="branches__label">It can reach out to</p>
      <ul className="branches__list">
        {BRANCHES.map((branch) => {
          const state = view?.branches[branch.id] ?? 'idle';
          return (
            <li key={branch.id} className="branch" data-state={state}>
              <span className="branch__head">
                <span className="branch__title">{branch.title}</span>
                <span className="branch__via">{branch.via}</span>
              </span>
              <span className="branch__text">{branch.text}</span>
              {state !== 'idle' ? <span className="branch__state">{BRANCH_STATE_TEXT[state]}</span> : null}
            </li>
          );
        })}
      </ul>
      {tool && running ? (
        <div className="branches__doing" key={tool.id}>
          <p>{running.doing}…</p>
          <code className="tech">
            {/* A break opportunity after "(" so long arguments wrap there, not mid-word. */}
            {running.via} · {formatCall(tool.name, tool.arguments).replace('(', '(\u200b')}
          </code>
        </div>
      ) : null}
    </div>
  );
}

const MODEL_PHASES: readonly { step: Step; label: string }[] = [
  { step: 'hearing', label: 'Listens' },
  { step: 'thinking', label: 'Thinks' },
  { step: 'speaking', label: 'Talks' },
];

/** Inside the single speech-to-speech step: which of its three jobs the model is doing now. */
function ModelPhases({ step }: { step: Step | null }) {
  return (
    <ol className="phases" aria-label="What the model is doing">
      {MODEL_PHASES.map((phase) => (
        <li key={phase.step} className="phases__item" data-active={step === phase.step || undefined}>
          {phase.label}
        </li>
      ))}
    </ol>
  );
}

interface CallFlowProps {
  mode: Mode;
  models: FlowModels;
  /** Live state during a call; null before one, when the diagram just explains the system. */
  view: LiveView | null;
  lastTurn: TurnEntry | null;
}

const after = (value: number | null | undefined, phrase: string) =>
  value === null || value === undefined ? null : `${phrase} ${formatMs(value)}`;

/**
 * How one turn of the conversation moves through the agent, top to bottom. Before a call it
 * explains the selected architecture; during one, the step happening right now lights up.
 */
export function CallFlow({ mode, models, view, lastTurn }: CallFlowProps) {
  const is = (...steps: Step[]) => !!view?.step && steps.includes(view.step);
  const sideSystems = <SideSystems view={view} />;

  return (
    <ol className="call-flow" data-mode={mode} data-live={view ? true : undefined}>
      <Stage
        title="You talk"
        text="Your voice streams to the agent over a live audio connection, the same kind video calls use."
        tag="WebRTC"
        active={is('hearing')}
      />
      {mode === 'pipeline' ? (
        <>
          <Stage
            title="It writes down what you say"
            text="Speech-to-text turns your voice into words while you are still talking."
            tag={models.stt ?? 'Deepgram'}
            active={is('hearing')}
          >
            {view?.heard && is('hearing', 'waiting') ? <p className="heard">“{view.heard}”</p> : null}
          </Stage>
          <Stage
            title="It notices you’ve finished"
            text="A small model tells a pause for breath from the end of your sentence."
            tag={models.turns}
            metric={after(lastTurn?.eou, 'waited')}
            active={is('waiting')}
          />
          <Stage
            title="It works out the reply"
            text="A language model decides what to say, and whether to look something up first."
            tag={models.llm}
            metric={after(lastTurn?.llmTtft, 'first words in')}
            active={is('thinking')}
          >
            {sideSystems}
          </Stage>
          <Stage
            title="It says it out loud"
            text="Text-to-speech starts talking before the whole sentence is written."
            tag={models.tts ?? 'Cartesia'}
            metric={after(lastTurn?.ttsTtfb, 'voice started in')}
            active={is('speaking')}
          />
        </>
      ) : (
        <Stage
          title="One model listens, thinks and talks"
          text="It takes your audio in and answers with audio, with no text step in between. It also decides when you’ve finished talking."
          tag={models.llm}
          metric={after(lastTurn?.e2e, 'replied in')}
          active={is('hearing', 'thinking', 'speaking')}
        >
          {view ? <ModelPhases step={view.step} /> : null}
          {sideSystems}
        </Stage>
      )}
      <Stage title="You hear the answer" text="The reply streams back to you as it is spoken." active={is('speaking')} />
    </ol>
  );
}
