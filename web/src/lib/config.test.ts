import { describe, expect, it } from 'vitest';
import { coerceCallConfig, DEFAULT_CALL_CONFIG, describeConfig } from './config.ts';
import { formatDuration, formatMs, humanizeIdentifier } from './format.ts';

describe('coerceCallConfig', () => {
  it('keeps valid values', () => {
    const config = { mode: 'pipeline', language: 'ja' };
    expect(coerceCallConfig(config)).toEqual(config);
  });

  it('falls back field by field for anything unexpected', () => {
    expect(coerceCallConfig({ mode: 'duplex', language: 'ja' })).toEqual({ ...DEFAULT_CALL_CONFIG, language: 'ja' });
    expect(coerceCallConfig({ mode: 'pipeline', language: 42 })).toEqual({ ...DEFAULT_CALL_CONFIG, mode: 'pipeline' });
    expect(coerceCallConfig(null)).toEqual(DEFAULT_CALL_CONFIG);
    expect(coerceCallConfig('realtime')).toEqual(DEFAULT_CALL_CONFIG);
  });

  it('only sends the contract fields', () => {
    expect(Object.keys(coerceCallConfig({ ...DEFAULT_CALL_CONFIG, room: 'call-x' })).sort()).toEqual([
      'language',
      'mode',
    ]);
  });

  it('describes the audio path', () => {
    expect(describeConfig(DEFAULT_CALL_CONFIG)).toContain('gpt-realtime-2');
    expect(describeConfig({ ...DEFAULT_CALL_CONFIG, mode: 'pipeline' })).toContain('Deepgram');
  });

  it('prefers the models the agent reports', () => {
    const pipeline = { ...DEFAULT_CALL_CONFIG, mode: 'pipeline' as const };
    const session = {
      type: 'session_info' as const,
      mode: 'pipeline' as const,
      models: { stt: 'deepgram/nova-3:multi', llm: 'openai/gpt-4.1-mini', tts: 'elevenlabs/eleven_flash_v2_5' },
      language: 'auto',
    };
    expect(describeConfig(pipeline, session)).toBe(
      'Pipeline · deepgram/nova-3:multi → openai/gpt-4.1-mini → elevenlabs/eleven_flash_v2_5',
    );
    expect(
      describeConfig(DEFAULT_CALL_CONFIG, { ...session, mode: 'realtime', models: { llm: 'openai/gpt-realtime-9' } }),
    ).toBe('Speech-to-speech · openai/gpt-realtime-9');
    // A report for the other architecture (a stale event) is ignored.
    expect(describeConfig(DEFAULT_CALL_CONFIG, session)).toBe('Speech-to-speech · gpt-realtime-2');
  });
});

describe('format', () => {
  it('formats latencies', () => {
    expect(formatMs(null)).toBe('—');
    expect(formatMs(820.4)).toBe('820 ms');
    expect(formatMs(1240)).toBe('1.24 s');
  });

  it('formats call durations', () => {
    expect(formatDuration(7)).toBe('0:07');
    expect(formatDuration(222)).toBe('3:42');
    expect(formatDuration(3729)).toBe('1:02:09');
  });

  it('humanizes tool names', () => {
    expect(humanizeIdentifier('find_available_slots')).toBe('Find available slots');
    expect(humanizeIdentifier('llm_prompt_tokens')).toBe('LLM prompt tokens');
    expect(humanizeIdentifier('tts characters_count')).toBe('TTS characters count');
  });
});
