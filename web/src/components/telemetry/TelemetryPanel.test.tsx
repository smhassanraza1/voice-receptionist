import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { liveView } from '../../lib/backstage.ts';
import { initialTelemetry, type ReceptionistEvent, type Telemetry, telemetryReducer } from '../../lib/events.ts';
import { TelemetryPanel } from './TelemetryPanel.tsx';

function run(events: ReceptionistEvent[]): Telemetry {
  return events.reduce(
    (state, event, i) => telemetryReducer(state, { type: 'event', event, receivedAt: 1_000 + i * 100 }),
    initialTelemetry(),
  );
}

const pipelineCall = run([
  {
    type: 'session_info',
    mode: 'pipeline',
    models: { stt: 'nova-3', llm: 'gpt-4.1-mini', tts: 'sonic-3' },
    language: 'auto',
  },
  { type: 'turn_metrics', turn: 1, e2e_latency_ms: 820, eou_delay_ms: 310, llm_ttft_ms: 400, tts_ttfb_ms: 110 },
  { type: 'tool_call', name: 'find_available_slots', status: 'started', duration_ms: null, arguments: null },
  { type: 'tool_call', name: 'find_available_slots', status: 'ok', duration_ms: 140, arguments: null },
  { type: 'interruption', turn: 1 },
  { type: 'handoff', target: 'billing', reason: 'invoice' },
  { type: 'tool_call', name: 'ask_billing_department', status: 'started', duration_ms: null, arguments: null },
]);

/** The text a reader sees: tags dropped, invisible line-break hints removed. */
const visible = (html: string) => html.replace(/<[^>]+>/g, '').replaceAll('\u200b', '');

const render = (telemetry: Telemetry, live: boolean) =>
  renderToStaticMarkup(
    <TelemetryPanel
      telemetry={telemetry}
      requested={{ mode: 'realtime', language: 'auto' }}
      view={
        live
          ? liveView({
              agentState: 'thinking',
              callerSpeaking: false,
              transcript: [],
              tools: telemetry.tools,
              handoffs: telemetry.handoffs,
              staffOnLine: false,
            })
          : null
      }
      hasCalled
    />,
  );

describe('TelemetryPanel', () => {
  it('tells a live pipeline call in plain words', () => {
    const html = visible(render(pipelineCall, true));
    expect(html).toContain('Live from this call');
    // The agent's report wins over the requested mode, and its models label the steps.
    expect(html).toContain('It writes down what you say');
    expect(html).toContain('nova-3');
    expect(html).toContain('first words in 400 ms');
    expect(html).toContain('Asking the billing system…');
    // The engineering layer under the plain words.
    expect(html).toContain('A2A · ask_billing_department()');
    expect(html).toContain('MCP · find_available_slots');
    expect(html).toContain('turn 1 · end of turn 310 ms · first token 400 ms · first audio 110 ms');
    expect(html).toContain('agent handoff · receptionist → billing agent · “invoice”');
    expect(html).toContain('pipeline · nova-3 → gpt-4.1-mini → sonic-3');
    expect(html).toContain('billing agent'); // handled by
    expect(html).toContain('Checked open appointment times');
    expect(html).toContain('Now talking to the billing specialist');
    expect(html).toContain('You talked over it, so it stopped to listen');
    expect(html).toContain('820 ms');
    expect(html).not.toMatch(/TTFT|TTFB|EOU|p50/);
  });

  it('closes with the agent’s own summary', () => {
    const html = visible(
      render(
        telemetryReducer(pipelineCall, {
        type: 'event',
          event: { type: 'usage', summary: { turns: 6, median_e2e_latency_ms: 820, false_interruptions: 2 } },
          receivedAt: 9_000,
        }),
        false,
      ),
    );
    expect(html).toContain('turns 6 · median e2e latency 820 ms · false interruptions 2');
  });

  it('shows which job the one speech-to-speech model is doing', () => {
    const telemetry = run([
      { type: 'session_info', mode: 'realtime', models: { llm: 'gpt-realtime-2', turns: 'server VAD' }, language: 'auto' },
      { type: 'turn_metrics', turn: 1, e2e_latency_ms: 640, eou_delay_ms: null, llm_ttft_ms: null, tts_ttfb_ms: null },
    ]);
    const view = liveView({
      agentState: 'speaking',
      callerSpeaking: false,
      transcript: [],
      tools: [],
      handoffs: [],
      staffOnLine: false,
    });
    const html = renderToStaticMarkup(
      <TelemetryPanel telemetry={telemetry} requested={{ mode: 'pipeline', language: 'auto' }} view={view} hasCalled />,
    );
    expect(html).toContain('One model listens, thinks and talks');
    expect(html).not.toContain('It writes down what you say');
    expect(html).toContain('replied in 640 ms');
    expect(html).toContain('from the model deciding you’d finished');
    expect(html).toMatch(/<li class="phases__item" data-active="true">Talks<\/li>/);
  });

  it('explains the requested architecture before any call', () => {
    const html = renderToStaticMarkup(
      <TelemetryPanel
        telemetry={initialTelemetry()}
        requested={{ mode: 'realtime', language: 'auto' }}
        view={null}
        hasCalled={false}
      />,
    );
    expect(html).toContain('How it works');
    expect(html).toContain('One model listens, thinks and talks');
    expect(html).not.toContain('What it did');
  });

  it('keeps the last call after hang-up', () => {
    const html = render(pipelineCall, false);
    expect(html).toContain('From your last call');
    expect(html).not.toContain('data-active');
  });
});
