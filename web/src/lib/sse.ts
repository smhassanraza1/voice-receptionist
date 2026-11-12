/**
 * Minimal server-sent-events client over fetch().
 *
 * `EventSource` can't send headers, and the operator passcode belongs in a header rather than a
 * URL (URLs end up in logs and history). This reads the same wire format from a fetch stream.
 */

import { errorFromResponse } from './api.ts';

export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

/** Incremental parser for the text/event-stream format (WHATWG HTML §9.2.6). */
export class SseParser {
  private buffer = '';
  private data: string[] = [];
  private event = '';
  private id: string | null = null;
  /** Reconnection delay the server asked for via `retry:`, if any. */
  retryMs: number | null = null;

  feed(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const messages: SseMessage[] = [];
    // Lines end in \n, \r\n or a lone \r. Keep a trailing partial line (or a lone \r that may be
    // the first half of \r\n) for the next chunk.
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const ch = this.buffer[i];
      if (ch !== '\n' && ch !== '\r') continue;
      if (ch === '\r' && i === this.buffer.length - 1) break;
      const line = this.buffer.slice(start, i);
      if (ch === '\r' && this.buffer[i + 1] === '\n') i++;
      start = i + 1;
      const message = this.line(line);
      if (message) messages.push(message);
    }
    this.buffer = this.buffer.slice(start);
    return messages;
  }

  private line(line: string): SseMessage | null {
    if (line === '') {
      if (this.data.length === 0) {
        this.event = '';
        return null;
      }
      const message = { event: this.event || 'message', data: this.data.join('\n'), id: this.id };
      this.data = [];
      this.event = '';
      return message;
    }
    if (line.startsWith(':')) return null; // comment / keepalive
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'data':
        this.data.push(value);
        break;
      case 'event':
        this.event = value;
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retryMs = Number(value);
        break;
    }
    return null;
  }
}

export interface StreamOptions {
  headers?: Record<string, string>;
  signal: AbortSignal;
  /** Abort if nothing (not even a keepalive comment) arrives for this long. */
  idleTimeoutMs?: number;
  onOpen?: () => void;
  onMessage: (message: SseMessage) => void;
}

export class StreamIdleError extends Error {
  constructor() {
    super('The event stream went quiet');
    this.name = 'StreamIdleError';
  }
}

/**
 * Read an SSE endpoint until it closes. Resolves with the server's `retry:` hint on a clean end
 * of stream; rejects with an `ApiError` for HTTP errors, `StreamIdleError` when the connection
 * silently stalls, or the fetch/abort error otherwise.
 */
export async function readEventStream(url: string, options: StreamOptions): Promise<number | null> {
  const local = new AbortController();
  const forwardAbort = () => local.abort(options.signal.reason);
  if (options.signal.aborted) forwardAbort();
  options.signal.addEventListener('abort', forwardAbort, { once: true });

  let idleTimer: number | undefined;
  const armIdleTimer = () => {
    window.clearTimeout(idleTimer);
    if (options.idleTimeoutMs) {
      idleTimer = window.setTimeout(() => local.abort(new StreamIdleError()), options.idleTimeoutMs);
    }
  };

  try {
    armIdleTimer();
    const response = await fetch(url, {
      headers: { Accept: 'text/event-stream', ...options.headers },
      cache: 'no-store',
      signal: local.signal,
    });
    if (!response.ok) throw await errorFromResponse(response);
    if (!response.body) throw new Error('Streaming responses are not supported by this browser');
    options.onOpen?.();

    const parser = new SseParser();
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    try {
      for (;;) {
        armIdleTimer();
        const { value, done } = await reader.read();
        if (done) break;
        for (const message of parser.feed(value)) options.onMessage(message);
      }
    } finally {
      reader.releaseLock();
    }
    return parser.retryMs;
  } finally {
    window.clearTimeout(idleTimer);
    options.signal.removeEventListener('abort', forwardAbort);
  }
}
