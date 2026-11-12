import { describe, expect, it } from 'vitest';
import { SseParser } from './sse.ts';

describe('SseParser', () => {
  it('parses named events and ignores comments', () => {
    const parser = new SseParser();
    const messages = parser.feed('retry: 3000\n\n: keepalive\n\nevent: snapshot\ndata: {"escalations":[]}\n\n');
    expect(messages).toEqual([{ event: 'snapshot', data: '{"escalations":[]}', id: null }]);
    expect(parser.retryMs).toBe(3000);
  });

  it('reassembles frames split across chunks, including a split CRLF', () => {
    const parser = new SseParser();
    expect(parser.feed('event: escal')).toEqual([]);
    expect(parser.feed('ation\r\ndata: {"a":')).toEqual([]);
    expect(parser.feed('1}\r')).toEqual([]);
    expect(parser.feed('\n\r\n')).toEqual([{ event: 'escalation', data: '{"a":1}', id: null }]);
  });

  it('joins multi-line data and defaults the event name', () => {
    const parser = new SseParser();
    expect(parser.feed('id: 7\ndata: one\ndata:two\n\n')).toEqual([
      { event: 'message', data: 'one\ntwo', id: '7' },
    ]);
  });

  it('drops a frame with no data lines', () => {
    const parser = new SseParser();
    expect(parser.feed('event: ping\n\ndata: x\n\n')).toEqual([{ event: 'message', data: 'x', id: null }]);
  });
});
