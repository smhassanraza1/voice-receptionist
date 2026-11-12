import { describe, expect, it } from 'vitest';
import { ApiError, type Escalation, parseRetryAfter } from '../lib/api.ts';
import { reconnectDelay, shownEscalation } from './useEscalationFeed.ts';

const escalation = (id: string): Escalation => ({
  id,
  room: `call-${id}`,
  reason: 'Wants a person',
  summary: '',
  created_at: '2026-11-02T09:00:00Z',
  status: 'open',
  updated_at: null,
  resolved_at: null,
});

describe('parseRetryAfter', () => {
  it('reads delay-seconds and HTTP dates', () => {
    expect(parseRetryAfter('120')).toBe(120_000);
    const now = Date.parse('2026-11-02T09:00:00Z');
    expect(parseRetryAfter('Mon, 02 Nov 2026 09:00:30 GMT', now)).toBe(30_000);
    expect(parseRetryAfter('Mon, 02 Nov 2026 08:00:00 GMT', now)).toBe(0);
  });

  it('ignores a missing or garbled header', () => {
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter('soon')).toBeNull();
  });
});

describe('reconnectDelay', () => {
  it('waits as long as a 429 asks, past the usual backoff cap', () => {
    const limited = new ApiError(429, 'rate_limited', 'Too many failed attempts', 300_000);
    expect(reconnectDelay(limited, 3000, 0)).toBe(300_000);
  });

  it('backs off exponentially from the retry hint otherwise', () => {
    expect(reconnectDelay(null, 3000, 0)).toBe(3000);
    expect(reconnectDelay(new Error('network'), 3000, 1)).toBe(6000);
    expect(reconnectDelay(null, null, 10)).toBe(15_000);
    expect(reconnectDelay(new ApiError(429, 'rate_limited', 'x'), 1000, 0)).toBe(1000);
  });
});

describe('shownEscalation', () => {
  const a = escalation('a');

  it('follows the list', () => {
    expect(shownEscalation([a], 'a', null, false)).toBe(a);
    expect(shownEscalation([a], null, a, true)).toBeNull();
  });

  it('keeps the escalation of a live call when a snapshot drops it', () => {
    expect(shownEscalation([], 'a', a, true)).toBe(a);
  });

  it('lets it go once the operator is off the call', () => {
    expect(shownEscalation([], 'a', a, false)).toBeNull();
    expect(shownEscalation([], 'a', escalation('b'), true)).toBeNull();
  });
});
