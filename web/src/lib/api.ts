/** Typed client for the web/token server (`python -m receptionist.web`). */

import type { CallConfig } from './config.ts';

export interface ConnectionDetails {
  serverUrl: string;
  participantToken: string;
  roomName: string;
  participantName: string;
}

export type EscalationStatus = 'open' | 'resolved';

export interface Escalation {
  id: string;
  room: string;
  reason: string;
  summary: string;
  created_at: string;
  status: EscalationStatus;
  updated_at: string | null;
  resolved_at: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** How long the server asked us to wait (`Retry-After`), if it did. */
  readonly retryAfterMs: number | null;

  constructor(status: number, code: string, message: string, retryAfterMs: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retryAfterMs = retryAfterMs;
  }
}

/** `Retry-After` as milliseconds from `now`: delay-seconds or an HTTP date (RFC 9110 §10.2.3). */
export function parseRetryAfter(value: string | null, now = Date.now()): number | null {
  if (value === null) return null;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed) * 1000;
  const at = Date.parse(trimmed);
  return Number.isNaN(at) ? null : Math.max(0, at - now);
}

export const OPERATOR_PASSCODE_HEADER = 'X-Operator-Passcode';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export async function errorFromResponse(response: Response): Promise<ApiError> {
  let code = 'http_error';
  let message = `Request failed (${response.status})`;
  try {
    const body: unknown = await response.json();
    if (isRecord(body)) {
      if (typeof body.error === 'string') code = body.error;
      if (typeof body.message === 'string') message = body.message;
    }
  } catch {
    // Not JSON (e.g. a proxy error page): keep the generic message.
  }
  return new ApiError(response.status, code, message, parseRetryAfter(response.headers.get('Retry-After')));
}

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { Accept: 'application/json', ...init.headers },
      cache: 'no-store',
    });
  } catch {
    throw new ApiError(0, 'network_error', "Can't reach the clinic's server. Is it running?");
  }
  if (!response.ok) throw await errorFromResponse(response);
  return response.json();
}

function asConnectionDetails(value: unknown): ConnectionDetails {
  if (
    isRecord(value) &&
    typeof value.serverUrl === 'string' &&
    typeof value.participantToken === 'string' &&
    typeof value.roomName === 'string' &&
    typeof value.participantName === 'string'
  ) {
    return {
      serverUrl: value.serverUrl,
      participantToken: value.participantToken,
      roomName: value.roomName,
      participantName: value.participantName,
    };
  }
  throw new ApiError(502, 'bad_response', 'The token server returned an unexpected response');
}

export function isEscalation(value: unknown): value is Escalation {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.room === 'string' &&
    typeof value.reason === 'string' &&
    typeof value.summary === 'string' &&
    typeof value.created_at === 'string' &&
    (value.status === 'open' || value.status === 'resolved')
  );
}

function asEscalation(value: unknown): Escalation {
  if (!isEscalation(value)) {
    throw new ApiError(502, 'bad_response', 'The server returned an unexpected escalation');
  }
  return { ...value, updated_at: value.updated_at ?? null, resolved_at: value.resolved_at ?? null };
}

const jsonPost = (body: unknown, headers: Record<string, string> = {}): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...headers },
  body: JSON.stringify(body),
});

/** A token for a new browser call; the server dispatches the receptionist with `config`. */
export async function fetchCallerToken(config: CallConfig): Promise<ConnectionDetails> {
  return asConnectionDetails(await request('/api/token', jsonPost(config)));
}

export async function fetchOperatorToken(args: {
  room: string;
  passcode: string;
  name: string;
}): Promise<ConnectionDetails> {
  return asConnectionDetails(await request('/api/operator/token', jsonPost(args)));
}

export async function listEscalations(passcode: string): Promise<Escalation[]> {
  const body = await request('/api/escalations', {
    headers: { [OPERATOR_PASSCODE_HEADER]: passcode },
  });
  if (!isRecord(body) || !Array.isArray(body.escalations)) {
    throw new ApiError(502, 'bad_response', 'The server returned an unexpected escalation list');
  }
  return body.escalations.map(asEscalation);
}

export async function resolveEscalation(id: string, passcode: string): Promise<Escalation> {
  const body = await request(
    `/api/escalations/${encodeURIComponent(id)}/resolve`,
    jsonPost({}, { [OPERATOR_PASSCODE_HEADER]: passcode }),
  );
  return asEscalation(body);
}

export function errorMessage(error: unknown, fallback = 'Something went wrong'): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}
