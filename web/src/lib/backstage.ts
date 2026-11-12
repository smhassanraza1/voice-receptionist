/**
 * What the agent is doing, for the "Behind the scenes" panel, in two layers: plain words anyone
 * follows, and the engineering detail underneath (the real tool, how it is reached, which agent
 * owns the call, the raw LiveKit states).
 */

import type { AgentState } from '@livekit/components-react';
import { type HandoffEntry, activeHandoff, type SessionInfoEvent, type ToolEntry } from './events.ts';
import { humanizeIdentifier } from './format.ts';
import type { TranscriptEntry } from './transcript.ts';

/** The side systems the agent reaches out to while it works out a reply. */
export type Branch = 'clinic' | 'billing' | 'person';

/** How a tool is reached: the clinic's MCP server, the billing agent over A2A, and so on. */
export type ToolRoute = 'MCP' | 'A2A' | 'agent handoff' | 'escalation' | 'built-in';

export interface ToolDescription {
  /** Present tense, while it runs: "Checking open appointment times". */
  doing: string;
  /** Past tense, once it finished: "Checked open appointment times". */
  done: string;
  /** The side system it reaches, or null for the agent's own actions (hanging up). */
  branch: Branch | null;
  via: ToolRoute;
}

const mcp = (doing: string, done: string): ToolDescription => ({ doing, done, branch: 'clinic', via: 'MCP' });

const TOOLS: Record<string, ToolDescription> = {
  search_knowledge_base: mcp('Searching the clinic’s help pages', 'Searched the clinic’s help pages'),
  list_providers: mcp('Looking up the doctors', 'Looked up the doctors'),
  find_available_slots: mcp('Checking open appointment times', 'Checked open appointment times'),
  book_appointment: mcp('Booking the appointment', 'Booked the appointment'),
  lookup_appointments: mcp(
    'Checking who’s calling and finding their visits',
    'Checked who’s calling and found their visits',
  ),
  reschedule_appointment: mcp('Moving the appointment', 'Moved the appointment'),
  cancel_appointment: mcp('Cancelling the appointment', 'Cancelled the appointment'),
  transfer_to_billing: {
    doing: 'Passing the call to the billing specialist',
    done: 'Passed the call to the billing specialist',
    branch: 'billing',
    via: 'agent handoff',
  },
  ask_billing_department: {
    doing: 'Asking the billing system',
    done: 'Asked the billing system',
    branch: 'billing',
    via: 'A2A',
  },
  return_to_reception: {
    doing: 'Passing the call back to the receptionist',
    done: 'Passed the call back to the receptionist',
    branch: 'billing',
    via: 'agent handoff',
  },
  transfer_to_human: {
    doing: 'Asking a staff member to join',
    done: 'Asked a staff member to join',
    branch: 'person',
    via: 'escalation',
  },
  end_call: { doing: 'Hanging up', done: 'Hung up', branch: null, via: 'built-in' },
};

export function describeTool(name: string): ToolDescription {
  const known = TOOLS[name];
  if (known) return known;
  // The clinic tools are discovered from the MCP server, so a new one is most likely there.
  return mcp(humanizeIdentifier(name), humanizeIdentifier(name));
}

/**
 * A tool call as code, the way the model made it: `find_available_slots(day="2026-10-31")`.
 * Long values are cut so the line stays one line; the full arguments sit behind a disclosure.
 */
export function formatCall(name: string, args: Record<string, unknown> | null): string {
  const parts = Object.entries(args ?? {}).map(([key, value]) => {
    const text = JSON.stringify(value) ?? String(value);
    return `${key}=${text.length > 28 ? `${text.slice(0, 27).trimEnd()}…` : text}`;
  });
  return `${name}(${parts.join(', ')})`;
}

/** The model path in one line: "deepgram/nova-3 → gpt-4.1-mini → cartesia/sonic-3". */
export function describeModels(info: SessionInfoEvent): string {
  const { stt, llm, tts } = info.models;
  const path = info.mode === 'pipeline' ? [stt, llm, tts] : [llm];
  return path.filter((m): m is string => !!m && m !== 'built-in').join(' → ');
}

/** Where the call is right now, in the order a turn moves through. */
export type Step = 'hearing' | 'waiting' | 'thinking' | 'speaking';

/** Which agent is answering the caller. */
export type Owner = 'receptionist' | 'billing' | 'staff';

export interface LiveSignals {
  agentState: AgentState;
  /** The caller's microphone is picking up speech right now. */
  callerSpeaking: boolean;
  transcript: readonly TranscriptEntry[];
  tools: readonly ToolEntry[];
  handoffs: readonly HandoffEntry[];
  /** A staff member has joined the call. */
  staffOnLine: boolean;
}

export type BranchState = 'idle' | 'busy' | 'connected';

export interface LiveView {
  step: Step | null;
  branches: Record<Branch, BranchState>;
  /** The tool running right now, if any. */
  running: ToolEntry | null;
  owner: Owner;
  agentState: AgentState;
  callerSpeaking: boolean;
  /** The caller's words being transcribed for the current turn, until the agent answers. */
  heard: string | null;
}

const ON_CALL: ReadonlySet<AgentState> = new Set(['listening', 'thinking', 'speaking', 'idle']);

export function liveView(signals: LiveSignals): LiveView {
  const running = signals.tools.findLast((t) => t.status === 'running') ?? null;
  const handoff = activeHandoff(signals.handoffs);
  const last = signals.transcript.at(-1) ?? null;

  const branches: Record<Branch, BranchState> = { clinic: 'idle', billing: 'idle', person: 'idle' };
  if (handoff?.target === 'billing') branches.billing = 'connected';
  if (handoff?.target === 'human') branches.person = 'busy';
  if (signals.staffOnLine) branches.person = 'connected';
  const branch = running ? describeTool(running.name).branch : null;
  if (branch && branches[branch] !== 'connected') branches[branch] = 'busy';

  let step: Step | null = null;
  if (signals.staffOnLine || !ON_CALL.has(signals.agentState)) step = null;
  else if (signals.callerSpeaking) step = 'hearing';
  else if (signals.agentState === 'speaking') step = 'speaking';
  else if (signals.agentState === 'thinking' || running) step = 'thinking';
  // The caller finished a sentence and the agent hasn't started on it yet: it is deciding
  // whether that pause was the end of the turn.
  else if (last?.speaker === 'you') step = 'waiting';

  const owner: Owner = signals.staffOnLine ? 'staff' : handoff?.target === 'billing' ? 'billing' : 'receptionist';

  return {
    step,
    branches,
    running,
    owner,
    agentState: signals.agentState,
    callerSpeaking: signals.callerSpeaking,
    heard: last?.speaker === 'you' ? last.text : null,
  };
}
