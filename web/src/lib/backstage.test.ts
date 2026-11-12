import { describe, expect, it } from 'vitest';
import { describeModels, describeTool, formatCall, type LiveSignals, liveView } from './backstage.ts';
import type { ToolEntry } from './events.ts';
import type { Speaker, TranscriptEntry } from './transcript.ts';

const tool = (name: string, status: ToolEntry['status'], id = 1): ToolEntry => ({
  id,
  name,
  status,
  durationMs: status === 'running' ? null : 120,
  arguments: null,
  startedAt: 0,
});

const said = (speaker: Speaker, text = 'Whoever is free on Saturday'): TranscriptEntry[] => [
  { id: '1', speaker, label: speaker, text, timestamp: 0, interim: false },
];

const signals = (patch: Partial<LiveSignals> = {}): LiveSignals => ({
  agentState: 'listening',
  callerSpeaking: false,
  transcript: [],
  tools: [],
  handoffs: [],
  staffOnLine: false,
  ...patch,
});

describe('describeTool', () => {
  it('words every clinic tool for a non-engineer', () => {
    expect(describeTool('find_available_slots')).toEqual({
      doing: 'Checking open appointment times',
      done: 'Checked open appointment times',
      branch: 'clinic',
      via: 'MCP',
    });
    expect(describeTool('ask_billing_department')).toMatchObject({ branch: 'billing', via: 'A2A' });
    expect(describeTool('transfer_to_billing').via).toBe('agent handoff');
    expect(describeTool('transfer_to_human').branch).toBe('person');
    expect(describeTool('end_call').branch).toBeNull();
  });

  it('falls back to a readable name for a tool it does not know', () => {
    expect(describeTool('check_parking_spaces')).toEqual({
      doing: 'Check parking spaces',
      done: 'Check parking spaces',
      branch: 'clinic',
      via: 'MCP',
    });
  });
});

describe('formatCall', () => {
  it('writes the call as code and cuts long values', () => {
    expect(formatCall('find_available_slots', { specialty: 'pediatrics', limit: 3 })).toBe(
      'find_available_slots(specialty="pediatrics", limit=3)',
    );
    expect(formatCall('search_knowledge_base', { query: 'do you take northwind hmo insurance plans' })).toBe(
      'search_knowledge_base(query="do you take northwind hmo…)',
    );
    expect(formatCall('list_providers', null)).toBe('list_providers()');
  });
});

describe('describeModels', () => {
  it('lists the pipeline stages and skips built-in parts of a realtime model', () => {
    const models = { stt: 'deepgram/nova-3', llm: 'gpt-4.1-mini', tts: 'cartesia/sonic-3' };
    expect(describeModels({ type: 'session_info', mode: 'pipeline', models, language: 'auto' })).toBe(
      'deepgram/nova-3 → gpt-4.1-mini → cartesia/sonic-3',
    );
    const realtime = { llm: 'gpt-realtime-2', stt: 'built-in', tts: 'built-in' };
    expect(describeModels({ type: 'session_info', mode: 'realtime', models: realtime, language: 'auto' })).toBe(
      'gpt-realtime-2',
    );
  });
});

describe('liveView', () => {
  it.each([
    ['the caller is talking', signals({ callerSpeaking: true, agentState: 'speaking' }), 'hearing'],
    ['the agent is talking', signals({ agentState: 'speaking' }), 'speaking'],
    ['the agent is thinking', signals({ agentState: 'thinking' }), 'thinking'],
    ['a tool is running', signals({ tools: [tool('book_appointment', 'running')] }), 'thinking'],
    ['the caller just finished a sentence', signals({ transcript: said('you') }), 'waiting'],
    ['the agent spoke last and waits', signals({ transcript: said('receptionist') }), null],
    ['the agent has not joined', signals({ agentState: 'connecting', callerSpeaking: true }), null],
    ['a staff member took over', signals({ staffOnLine: true, callerSpeaking: true }), null],
  ] as const)('picks the step when %s', (_label, input, step) => {
    expect(liveView(input).step).toBe(step);
  });

  it('lights the side system of the running tool', () => {
    const view = liveView(signals({ tools: [tool('find_available_slots', 'ok', 1), tool('ask_billing_department', 'running', 2)] }));
    expect(view.running?.id).toBe(2);
    expect(view.branches).toEqual({ clinic: 'idle', billing: 'busy', person: 'idle' });
  });

  it('keeps a handed-off system connected until the call comes back', () => {
    const billing = { id: 1, target: 'billing', reason: '', at: 0 } as const;
    expect(liveView(signals({ handoffs: [billing] })).branches.billing).toBe('connected');
    const back = [billing, { id: 2, target: 'receptionist', reason: '', at: 1 } as const];
    expect(liveView(signals({ handoffs: back })).branches.billing).toBe('idle');
  });

  it('says who owns the call and what it is hearing', () => {
    const billing = [{ id: 1, target: 'billing', reason: '', at: 0 } as const];
    expect(liveView(signals()).owner).toBe('receptionist');
    expect(liveView(signals({ handoffs: billing })).owner).toBe('billing');
    expect(liveView(signals({ handoffs: billing, staffOnLine: true })).owner).toBe('staff');
    expect(liveView(signals({ transcript: said('you', 'Saturday morning') })).heard).toBe('Saturday morning');
    expect(liveView(signals({ transcript: said('receptionist') })).heard).toBeNull();
  });

  it('shows a requested person as pending until they join', () => {
    const human = [{ id: 1, target: 'human', reason: '', at: 0 } as const];
    expect(liveView(signals({ handoffs: human })).branches.person).toBe('busy');
    expect(liveView(signals({ handoffs: human, staffOnLine: true })).branches.person).toBe('connected');
  });
});
