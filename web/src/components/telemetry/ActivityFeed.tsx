import { Fragment, type ReactNode } from 'react';
import { describeModels, describeTool } from '../../lib/backstage.ts';
import type { ActivityEntry, HandoffTarget, ToolEntry, TurnEntry } from '../../lib/events.ts';
import { formatClock, formatMs } from '../../lib/format.ts';
import { AlertIcon, CheckIcon, HeadsetIcon, PhoneIcon, ReceiptIcon, SpeakerIcon, Spinner, XIcon } from '../Icon.tsx';

const HANDOFF: Record<HandoffTarget, { text: string; route: string }> = {
  billing: { text: 'Now talking to the billing specialist', route: 'agent handoff · receptionist → billing agent' },
  human: { text: 'Called for a staff member', route: 'escalation · operator console notified' },
  receptionist: { text: 'Back with the receptionist', route: 'agent handoff · billing agent → receptionist' },
};

const TURN_PARTS: readonly [keyof TurnEntry, string][] = [
  ['eou', 'end of turn'],
  ['llmTtft', 'first token'],
  ['ttsTtfb', 'first audio'],
];

/** Plain words on top for anyone; the engineering detail underneath for whoever wants it. */
interface Line {
  icon: ReactNode;
  text: ReactNode;
  tech: string;
  detail: string | null;
  tone: 'plain' | 'good' | 'bad' | 'handoff';
  args?: Record<string, unknown>;
}

interface Lookups {
  tools: ReadonlyMap<number, ToolEntry>;
  turns: ReadonlyMap<number, TurnEntry>;
}

function describe(entry: ActivityEntry, { tools, turns }: Lookups): Line | null {
  switch (entry.kind) {
    case 'session': {
      const { info } = entry;
      const turnDetection = info.models.turns ? ` · turns: ${info.models.turns}` : '';
      return {
        icon: <PhoneIcon size={12} />,
        text: 'Call connected',
        tech: `${info.mode === 'realtime' ? 'speech-to-speech' : 'pipeline'} · ${describeModels(info)}${turnDetection}`,
        detail: null,
        tone: 'plain',
      };
    }
    case 'turn': {
      const timing = turns.get(entry.turn);
      const parts = TURN_PARTS.flatMap(([key, label]) => {
        const value = timing?.[key];
        return typeof value === 'number' ? [`${label} ${formatMs(value)}`] : [];
      });
      return {
        icon: <SpeakerIcon size={13} />,
        text: 'Answered you',
        tech: [`turn ${entry.turn}`, ...parts].join(' · '),
        detail: entry.e2e === null ? null : `in ${formatMs(entry.e2e)}`,
        tone: 'plain',
      };
    }
    case 'interruption':
      return {
        icon: <AlertIcon size={13} />,
        text: 'You talked over it, so it stopped to listen',
        tech: `interruption${entry.turn === null ? '' : ` · turn ${entry.turn}`} · agent audio cut`,
        detail: null,
        tone: 'plain',
      };
    case 'handoff': {
      const handoff = HANDOFF[entry.target];
      return {
        icon: entry.target === 'billing' ? <ReceiptIcon size={13} /> : <HeadsetIcon size={13} />,
        text: handoff.text,
        tech: entry.reason ? `${handoff.route} · “${entry.reason}”` : handoff.route,
        detail: null,
        tone: 'handoff',
      };
    }
    case 'tool': {
      const tool = tools.get(entry.toolId);
      if (!tool) return null;
      const words = describeTool(tool.name);
      const args = tool.arguments && Object.keys(tool.arguments).length > 0 ? tool.arguments : undefined;
      const tech = `${words.via} · ${tool.name}`;
      if (tool.status === 'running') {
        return { icon: <Spinner size={11} />, text: `${words.doing}…`, tech, detail: null, tone: 'plain', args };
      }
      return {
        icon: tool.status === 'ok' ? <CheckIcon size={13} strokeWidth={2.4} /> : <XIcon size={12} strokeWidth={2.4} />,
        text: tool.status === 'ok' ? words.done : `${words.doing}: failed`,
        tech: tool.status === 'ok' ? tech : `${tech} · error`,
        detail: formatMs(tool.durationMs),
        tone: tool.status === 'ok' ? 'good' : 'bad',
        args,
      };
    }
    case 'summary': {
      // e.g. {turns: 6, median_e2e_latency_ms: 820} -> "turns 6 · median e2e latency 820 ms"
      const counts = Object.entries(entry.summary).flatMap(([key, value]) => {
        if (typeof value !== 'number') return [];
        const ms = key.endsWith('_ms');
        return [`${key.replace(/_ms$/, '').replaceAll('_', ' ')} ${ms ? formatMs(value) : value}`];
      });
      return {
        icon: <CheckIcon size={13} strokeWidth={2.4} />,
        text: 'The agent’s own summary of the call',
        tech: counts.join(' · '),
        detail: null,
        tone: 'good',
      };
    }
  }
}

const SHOWN = 10;

/** The call's story so far, newest first. */
export function ActivityFeed({
  activity,
  tools,
  turns,
}: {
  activity: readonly ActivityEntry[];
  tools: readonly ToolEntry[];
  turns: readonly TurnEntry[];
}) {
  const lookups: Lookups = {
    tools: new Map(tools.map((t) => [t.id, t])),
    turns: new Map(turns.map((t) => [t.turn, t])),
  };
  const lines = activity
    .toReversed()
    .flatMap((entry) => {
      const line = describe(entry, lookups);
      return line ? [{ entry, line }] : [];
    })
    .slice(0, SHOWN);

  if (lines.length === 0) {
    return <p className="empty-hint">Each answer, lookup and handoff is listed here as it happens.</p>;
  }
  return (
    <ol className="activity" aria-label="What the agent did, newest first">
      {lines.map(({ entry, line }) => (
        <li key={entry.id} className="activity__item" data-tone={line.tone}>
          <span className="activity__icon" aria-hidden="true">
            {line.icon}
          </span>
          <div className="activity__body">
            <span className="activity__text">{line.text}</span>
            <code className="tech">
              {line.tech.split(' · ').map((part, i) => (
                // Short parts ("first token 390 ms") never break inside; long ones wrap freely.
                <Fragment key={i}>
                  {i > 0 ? ' · ' : null}
                  <span className={part.length <= 28 ? 'tech__part' : undefined}>{part}</span>
                </Fragment>
              ))}
            </code>
            {line.args ? (
              <details className="activity__args">
                <summary>arguments</summary>
                <pre>{JSON.stringify(line.args, null, 2)}</pre>
              </details>
            ) : null}
          </div>
          {line.detail ? <span className="activity__detail">{line.detail}</span> : null}
          <time className="activity__time" dateTime={new Date(entry.at).toISOString()}>
            {formatClock(entry.at)}
          </time>
        </li>
      ))}
    </ol>
  );
}
