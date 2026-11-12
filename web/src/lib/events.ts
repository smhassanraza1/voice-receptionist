/**
 * `receptionist.events`: JSON the voice agent publishes on a reliable data topic
 * (see `src/receptionist/agent/events.py`). Parsed defensively: unknown types and
 * malformed fields are dropped rather than trusted.
 */

import { median } from './stats.ts';

export const RECEPTIONIST_EVENTS_TOPIC = 'receptionist.events';

export type HandoffTarget = 'human' | 'billing' | 'receptionist';
export type ToolStatus = 'started' | 'ok' | 'error';

export interface SessionInfoEvent {
  type: 'session_info';
  mode: 'realtime' | 'pipeline';
  models: { llm?: string; stt?: string; tts?: string; turns?: string };
  language: string;
}

export interface TurnMetricsEvent {
  type: 'turn_metrics';
  turn: number;
  e2e_latency_ms: number | null;
  eou_delay_ms: number | null;
  llm_ttft_ms: number | null;
  tts_ttfb_ms: number | null;
}

export interface ToolCallEvent {
  type: 'tool_call';
  name: string;
  status: ToolStatus;
  duration_ms: number | null;
  arguments: Record<string, unknown> | null;
}

export interface InterruptionEvent {
  type: 'interruption';
  turn: number | null;
}

export interface HandoffEvent {
  type: 'handoff';
  target: HandoffTarget;
  reason: string;
}

export interface UsageEvent {
  type: 'usage';
  summary: Record<string, unknown>;
}

export type ReceptionistEvent =
  | SessionInfoEvent
  | TurnMetricsEvent
  | ToolCallEvent
  | InterruptionEvent
  | HandoffEvent
  | UsageEvent;

// -- parsing ---------------------------------------------------------------------------------

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);

/** A finite, non-negative number, or null (the agent omits metrics a mode doesn't have). */
function ms(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
}

function str(v: unknown, max = 200): string | null {
  return typeof v === 'string' && v.length > 0 ? v.slice(0, max) : null;
}

function optionalModels(v: unknown): SessionInfoEvent['models'] {
  if (!isObject(v)) return {};
  const models: SessionInfoEvent['models'] = {};
  for (const key of ['llm', 'stt', 'tts', 'turns'] as const) {
    const value = str(v[key], 120);
    if (value) models[key] = value;
  }
  return models;
}

export function parseEventObject(raw: unknown): ReceptionistEvent | null {
  if (!isObject(raw)) return null;
  switch (raw.type) {
    case 'session_info': {
      if (raw.mode !== 'realtime' && raw.mode !== 'pipeline') return null;
      return {
        type: 'session_info',
        mode: raw.mode,
        models: optionalModels(raw.models),
        language: str(raw.language, 16) ?? 'auto',
      };
    }
    case 'turn_metrics': {
      const turn = raw.turn;
      if (typeof turn !== 'number' || !Number.isInteger(turn) || turn < 0) return null;
      return {
        type: 'turn_metrics',
        turn,
        e2e_latency_ms: ms(raw.e2e_latency_ms),
        eou_delay_ms: ms(raw.eou_delay_ms),
        llm_ttft_ms: ms(raw.llm_ttft_ms),
        tts_ttfb_ms: ms(raw.tts_ttfb_ms),
      };
    }
    case 'tool_call': {
      const name = str(raw.name, 80);
      const status = raw.status;
      if (!name || (status !== 'started' && status !== 'ok' && status !== 'error')) return null;
      return {
        type: 'tool_call',
        name,
        status,
        duration_ms: ms(raw.duration_ms),
        arguments: isObject(raw.arguments) ? raw.arguments : null,
      };
    }
    case 'interruption': {
      const turn = typeof raw.turn === 'number' && Number.isInteger(raw.turn) ? raw.turn : null;
      return { type: 'interruption', turn };
    }
    case 'handoff': {
      const target = raw.target;
      if (target !== 'human' && target !== 'billing' && target !== 'receptionist') return null;
      return { type: 'handoff', target, reason: str(raw.reason, 500) ?? '' };
    }
    case 'usage':
      return isObject(raw.summary) ? { type: 'usage', summary: raw.summary } : null;
    default:
      return null;
  }
}

const decoder = new TextDecoder();

export function parseReceptionistEvent(payload: Uint8Array): ReceptionistEvent | null {
  try {
    return parseEventObject(JSON.parse(decoder.decode(payload)));
  } catch {
    return null;
  }
}

// -- telemetry state -------------------------------------------------------------------------

export type ToolEntryStatus = 'running' | 'ok' | 'error';

export interface ToolEntry {
  id: number;
  name: string;
  status: ToolEntryStatus;
  durationMs: number | null;
  arguments: Record<string, unknown> | null;
  startedAt: number;
}

export interface HandoffEntry {
  id: number;
  target: HandoffTarget;
  reason: string;
  at: number;
}

export interface TurnEntry {
  turn: number;
  e2e: number | null;
  eou: number | null;
  llmTtft: number | null;
  ttsTtfb: number | null;
}

/** One line of the call's story, in arrival order. Tool lines point at their `ToolEntry`. */
export type ActivityEntry =
  | { id: number; at: number; kind: 'session'; info: SessionInfoEvent }
  | { id: number; at: number; kind: 'turn'; turn: number; e2e: number | null }
  | { id: number; at: number; kind: 'tool'; toolId: number }
  | { id: number; at: number; kind: 'interruption'; turn: number | null }
  | { id: number; at: number; kind: 'handoff'; target: HandoffTarget; reason: string }
  | { id: number; at: number; kind: 'summary'; summary: Record<string, unknown> };

type WithoutId<T> = T extends unknown ? Omit<T, 'id'> : never;

export interface Telemetry {
  session: SessionInfoEvent | null;
  turns: TurnEntry[];
  tools: ToolEntry[];
  handoffs: HandoffEntry[];
  activity: ActivityEntry[];
  interruptions: number;
  usage: Record<string, unknown> | null;
  eventCount: number;
  nextId: number;
}

export const MAX_TURNS = 200;
export const MAX_TOOLS = 50;
export const MAX_ACTIVITY = 60;

export const initialTelemetry = (): Telemetry => ({
  session: null,
  turns: [],
  tools: [],
  handoffs: [],
  activity: [],
  interruptions: 0,
  usage: null,
  eventCount: 0,
  nextId: 1,
});

export type TelemetryAction =
  | { type: 'event'; event: ReceptionistEvent; receivedAt: number }
  | { type: 'reset' };

function log(state: Telemetry, entry: WithoutId<ActivityEntry>): Telemetry {
  const activity = [...state.activity, { ...entry, id: state.nextId } as ActivityEntry];
  return { ...state, nextId: state.nextId + 1, activity: activity.slice(-MAX_ACTIVITY) };
}

function addTool(state: Telemetry, entry: Omit<ToolEntry, 'id'>, at: number): Telemetry {
  const tools = [...state.tools, { ...entry, id: state.nextId }].slice(-MAX_TOOLS);
  return log({ ...state, nextId: state.nextId + 1, tools }, { kind: 'tool', toolId: state.nextId, at });
}

function applyToolCall(state: Telemetry, event: ToolCallEvent, at: number): Telemetry {
  if (event.status === 'started') {
    return addTool(
      state,
      { name: event.name, status: 'running', durationMs: null, arguments: event.arguments, startedAt: at },
      at,
    );
  }
  // Completion: close the most recent running call of the same tool.
  const index = state.tools.findLastIndex((t) => t.name === event.name && t.status === 'running');
  if (index === -1) {
    const startedAt = event.duration_ms === null ? at : at - event.duration_ms;
    return addTool(
      state,
      { name: event.name, status: event.status, durationMs: event.duration_ms, arguments: event.arguments, startedAt },
      startedAt,
    );
  }
  const tools = state.tools.slice();
  const running = tools[index]!;
  tools[index] = {
    ...running,
    status: event.status,
    durationMs: event.duration_ms ?? Math.max(0, at - running.startedAt),
    arguments: event.arguments ?? running.arguments,
  };
  return { ...state, tools };
}

function applyTurn(state: Telemetry, event: TurnMetricsEvent, at: number): Telemetry {
  const entry: TurnEntry = {
    turn: event.turn,
    e2e: event.e2e_latency_ms,
    eou: event.eou_delay_ms,
    llmTtft: event.llm_ttft_ms,
    ttsTtfb: event.tts_ttfb_ms,
  };
  // A turn reported twice (e.g. metrics completed late) replaces the earlier row.
  const turns = state.turns.filter((t) => t.turn !== event.turn);
  turns.push(entry);
  turns.sort((a, b) => a.turn - b.turn);
  const next = { ...state, turns: turns.slice(-MAX_TURNS) };
  const logged = state.activity.some((a) => a.kind === 'turn' && a.turn === event.turn);
  if (!logged) return log(next, { kind: 'turn', turn: event.turn, e2e: entry.e2e, at });
  const activity = state.activity.map((a) =>
    a.kind === 'turn' && a.turn === event.turn ? { ...a, e2e: entry.e2e } : a,
  );
  return { ...next, activity };
}

export function telemetryReducer(state: Telemetry, action: TelemetryAction): Telemetry {
  if (action.type === 'reset') return initialTelemetry();
  const { event, receivedAt } = action;
  const next = { ...state, eventCount: state.eventCount + 1 };
  switch (event.type) {
    case 'session_info':
      // Logged once, when the call connects; a repeat only refreshes what the panel shows.
      return state.activity.some((a) => a.kind === 'session')
        ? { ...next, session: event }
        : log({ ...next, session: event }, { kind: 'session', info: event, at: receivedAt });
    case 'turn_metrics':
      return applyTurn(next, event, receivedAt);
    case 'tool_call':
      return applyToolCall(next, event, receivedAt);
    case 'interruption':
      return log({ ...next, interruptions: next.interruptions + 1 }, { kind: 'interruption', turn: event.turn, at: receivedAt });
    case 'handoff':
      return log(
        {
          ...next,
          nextId: next.nextId + 1,
          handoffs: [
            ...next.handoffs,
            { id: next.nextId, target: event.target, reason: event.reason, at: receivedAt },
          ],
        },
        { kind: 'handoff', target: event.target, reason: event.reason, at: receivedAt },
      );
    case 'usage':
      return log({ ...next, usage: event.summary }, { kind: 'summary', summary: event.summary, at: receivedAt });
  }
}

// -- derived ---------------------------------------------------------------------------------

export interface LatencySummary {
  e2e: number | null;
  eou: number | null;
  llmTtft: number | null;
  ttsTtfb: number | null;
}

const present = (values: (number | null)[]): number[] =>
  values.filter((v): v is number => v !== null);

/** Running p50 of each latency component over every turn so far. */
export function latencyP50(turns: readonly TurnEntry[]): LatencySummary {
  return {
    e2e: median(present(turns.map((t) => t.e2e))),
    eou: median(present(turns.map((t) => t.eou))),
    llmTtft: median(present(turns.map((t) => t.llmTtft))),
    ttsTtfb: median(present(turns.map((t) => t.ttsTtfb))),
  };
}

/** The handoff currently in effect (a hand-back to the receptionist clears it). */
export function activeHandoff(handoffs: readonly HandoffEntry[]): HandoffEntry | null {
  const last = handoffs.at(-1);
  return last && last.target !== 'receptionist' ? last : null;
}
