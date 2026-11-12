import { describe, expect, it } from 'vitest';
import {
  activeHandoff,
  initialTelemetry,
  latencyP50,
  MAX_ACTIVITY,
  MAX_TOOLS,
  parseEventObject,
  parseReceptionistEvent,
  type ReceptionistEvent,
  type Telemetry,
  telemetryReducer,
} from './events.ts';

const encode = (value: unknown) => new TextEncoder().encode(JSON.stringify(value));

function run(events: ReceptionistEvent[], start = 1_000): Telemetry {
  return events.reduce(
    (state, event, i) => telemetryReducer(state, { type: 'event', event, receivedAt: start + i * 100 }),
    initialTelemetry(),
  );
}

describe('parseReceptionistEvent', () => {
  it('parses every contract event type', () => {
    const samples = [
      { type: 'session_info', mode: 'realtime', models: { llm: 'gpt-realtime-2' }, language: 'auto' },
      { type: 'turn_metrics', turn: 3, e2e_latency_ms: 820, eou_delay_ms: 310, llm_ttft_ms: 400, tts_ttfb_ms: 110 },
      { type: 'tool_call', name: 'find_available_slots', status: 'ok', duration_ms: 120, arguments: { limit: 5 } },
      { type: 'interruption', turn: 4 },
      { type: 'handoff', target: 'billing', reason: 'balance question' },
      { type: 'usage', summary: { llm_prompt_tokens: 1200 } },
    ];
    expect(samples.map((s) => parseReceptionistEvent(encode(s))?.type)).toEqual(samples.map((s) => s.type));
  });

  it('keeps missing latency components as null instead of inventing zeros', () => {
    const event = parseEventObject({ type: 'turn_metrics', turn: 1, e2e_latency_ms: 640 });
    expect(event).toEqual({
      type: 'turn_metrics',
      turn: 1,
      e2e_latency_ms: 640,
      eou_delay_ms: null,
      llm_ttft_ms: null,
      tts_ttfb_ms: null,
    });
  });

  it.each([
    ['not json', new TextEncoder().encode('{nope')],
    ['unknown type', encode({ type: 'mystery' })],
    ['bad mode', encode({ type: 'session_info', mode: 'duplex', models: {} })],
    ['bad tool status', encode({ type: 'tool_call', name: 'x', status: 'maybe' })],
    ['bad handoff target', encode({ type: 'handoff', target: 'pharmacy' })],
    ['non-integer turn', encode({ type: 'turn_metrics', turn: 1.5 })],
    ['array payload', encode([{ type: 'interruption' }])],
  ])('drops %s', (_label, payload) => {
    expect(parseReceptionistEvent(payload)).toBeNull();
  });

  it('rejects negative or non-finite latencies', () => {
    const event = parseEventObject({ type: 'turn_metrics', turn: 2, e2e_latency_ms: -5, llm_ttft_ms: 'fast' });
    expect(event).toMatchObject({ e2e_latency_ms: null, llm_ttft_ms: null });
  });
});

describe('telemetryReducer', () => {
  it('pairs tool starts with their completion and measures duration', () => {
    const state = run([
      { type: 'tool_call', name: 'find_available_slots', status: 'started', duration_ms: null, arguments: { limit: 3 } },
      { type: 'tool_call', name: 'book_appointment', status: 'started', duration_ms: null, arguments: null },
      { type: 'tool_call', name: 'find_available_slots', status: 'ok', duration_ms: 140, arguments: null },
      { type: 'tool_call', name: 'book_appointment', status: 'error', duration_ms: null, arguments: null },
    ]);
    expect(state.tools.map((t) => [t.name, t.status, t.durationMs])).toEqual([
      ['find_available_slots', 'ok', 140],
      ['book_appointment', 'error', 200], // measured from receipt times when not reported
    ]);
    expect(state.tools[0]!.arguments).toEqual({ limit: 3 }); // started args kept
  });

  it('records a completion that arrives without a start', () => {
    const state = run([{ type: 'tool_call', name: 'lookup_appointments', status: 'ok', duration_ms: 90, arguments: null }]);
    expect(state.tools).toHaveLength(1);
    expect(state.tools[0]).toMatchObject({ status: 'ok', durationMs: 90 });
  });

  it('bounds the tool history', () => {
    const many = Array.from({ length: MAX_TOOLS + 5 }, (_, i): ReceptionistEvent => ({
      type: 'tool_call',
      name: `tool_${i}`,
      status: 'ok',
      duration_ms: 1,
      arguments: null,
    }));
    const state = run(many);
    expect(state.tools).toHaveLength(MAX_TOOLS);
    expect(state.tools.at(-1)!.name).toBe(`tool_${MAX_TOOLS + 4}`);
  });

  it('replaces a re-reported turn and keeps turns ordered', () => {
    const turn = (n: number, e2e: number): ReceptionistEvent => ({
      type: 'turn_metrics',
      turn: n,
      e2e_latency_ms: e2e,
      eou_delay_ms: null,
      llm_ttft_ms: null,
      tts_ttfb_ms: null,
    });
    const state = run([turn(2, 900), turn(1, 700), turn(2, 950)]);
    expect(state.turns.map((t) => [t.turn, t.e2e])).toEqual([
      [1, 700],
      [2, 950],
    ]);
  });

  it('counts interruptions and tracks the active handoff', () => {
    const state = run([
      { type: 'interruption', turn: 2 },
      { type: 'interruption', turn: null },
      { type: 'handoff', target: 'billing', reason: 'invoice' },
    ]);
    expect(state.interruptions).toBe(2);
    expect(activeHandoff(state.handoffs)?.target).toBe('billing');

    const back = telemetryReducer(state, {
      type: 'event',
      event: { type: 'handoff', target: 'receptionist', reason: '' },
      receivedAt: 9_999,
    });
    expect(activeHandoff(back.handoffs)).toBeNull();
    expect(back.eventCount).toBe(4);
  });

  it('logs the call in arrival order, one line per tool call and turn', () => {
    const turn = (e2e: number): ReceptionistEvent => ({
      type: 'turn_metrics',
      turn: 1,
      e2e_latency_ms: e2e,
      eou_delay_ms: null,
      llm_ttft_ms: null,
      tts_ttfb_ms: null,
    });
    const state = run([
      { type: 'session_info', mode: 'pipeline', models: {}, language: 'auto' },
      { type: 'tool_call', name: 'find_available_slots', status: 'started', duration_ms: null, arguments: null },
      { type: 'tool_call', name: 'find_available_slots', status: 'ok', duration_ms: 140, arguments: null },
      turn(900),
      { type: 'interruption', turn: 1 },
      turn(880), // re-reported: updates its line instead of adding one
      { type: 'handoff', target: 'billing', reason: 'invoice' },
    ]);
    expect(state.activity.map((a) => a.kind)).toEqual(['session', 'tool', 'turn', 'interruption', 'handoff']);
    const [, tool, logged] = state.activity;
    expect(tool?.kind === 'tool' && state.tools.find((t) => t.id === tool.toolId)?.status).toBe('ok');
    expect(logged).toMatchObject({ kind: 'turn', turn: 1, e2e: 880, at: 1_300 });
    expect(new Set(state.activity.map((a) => a.id)).size).toBe(5);
  });

  it('bounds the activity log', () => {
    const many = Array.from({ length: MAX_ACTIVITY + 3 }, (): ReceptionistEvent => ({ type: 'interruption', turn: null }));
    expect(run(many).activity).toHaveLength(MAX_ACTIVITY);
  });

  it('resets', () => {
    const state = run([{ type: 'interruption', turn: 1 }]);
    expect(telemetryReducer(state, { type: 'reset' })).toEqual(initialTelemetry());
  });
});

describe('latencyP50', () => {
  it('takes the median of each component over the turns that report it', () => {
    const p50 = latencyP50([
      { turn: 1, e2e: 800, eou: 300, llmTtft: 380, ttsTtfb: null },
      { turn: 2, e2e: 1000, eou: 340, llmTtft: 420, ttsTtfb: null },
      { turn: 3, e2e: 600, eou: null, llmTtft: 360, ttsTtfb: null },
    ]);
    expect(p50).toEqual({ e2e: 800, eou: 320, llmTtft: 380, ttsTtfb: null });
  });
});
