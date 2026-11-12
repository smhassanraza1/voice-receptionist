/** Call options offered on the caller page. Mirrors `TokenRequest` in receptionist/web/models.py. */

import type { SessionInfoEvent } from './events.ts';

export type Mode = 'realtime' | 'pipeline';
export type Language = 'auto' | 'en' | 'es' | 'fr' | 'de' | 'hi' | 'ja';

export interface CallConfig {
  mode: Mode;
  language: Language;
}

export const DEFAULT_CALL_CONFIG: CallConfig = {
  mode: 'realtime',
  language: 'auto',
};

export interface ArchitectureOption {
  value: Mode;
  label: string;
  blurb: string;
}

export const ARCHITECTURES: readonly ArchitectureOption[] = [
  {
    value: 'realtime',
    label: 'Speech-to-speech',
    blurb: 'One AI model hears you and talks straight back, with no text step in between.',
  },
  {
    value: 'pipeline',
    label: 'Pipeline',
    blurb: 'Three models in a row: speech to text, a language model, then a voice. Each part can be swapped and timed.',
  },
];

/**
 * The speech-to-speech model the agent runs by default (`OPENAI_REALTIME_MODEL`), shown until the
 * agent's `session_info` reports what it actually runs.
 */
export const REALTIME_MODEL = 'gpt-realtime-2';

export interface LanguageOption {
  value: Language;
  /** Endonym, so speakers recognise their own language. */
  label: string;
  /** BCP 47 tag for the label itself (correct glyphs and screen-reader pronunciation). */
  lang?: string;
}

export const LANGUAGES: readonly LanguageOption[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'en', label: 'English', lang: 'en' },
  { value: 'es', label: 'Español', lang: 'es' },
  { value: 'fr', label: 'Français', lang: 'fr' },
  { value: 'de', label: 'Deutsch', lang: 'de' },
  { value: 'hi', label: 'हिन्दी', lang: 'hi' },
  { value: 'ja', label: '日本語', lang: 'ja' },
];

const MODES = new Set<string>(ARCHITECTURES.map((a) => a.value));
const LANGUAGE_VALUES = new Set<string>(LANGUAGES.map((l) => l.value));

/** Accepts anything (localStorage, token-source options) and returns a valid config. */
export function coerceCallConfig(value: unknown): CallConfig {
  const v = (typeof value === 'object' && value !== null ? value : {}) as Record<string, unknown>;
  const pick = <T extends string>(raw: unknown, allowed: Set<string>, fallback: T): T =>
    typeof raw === 'string' && allowed.has(raw) ? (raw as T) : fallback;
  return {
    mode: pick(v.mode, MODES, DEFAULT_CALL_CONFIG.mode),
    language: pick(v.language, LANGUAGE_VALUES, DEFAULT_CALL_CONFIG.language),
  };
}

const STORAGE_KEY = 'lakeside.callConfig';

export function loadCallConfig(): CallConfig {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return coerceCallConfig(raw ? JSON.parse(raw) : null);
  } catch {
    return DEFAULT_CALL_CONFIG;
  }
}

export function saveCallConfig(config: CallConfig): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch {
    // Private mode or storage disabled: the choice just isn't remembered.
  }
}

/** The call's audio path: the models the agent reported, or the defaults until it has. */
export function describeConfig(config: CallConfig, session: SessionInfoEvent | null = null): string {
  const models = session?.mode === config.mode ? session.models : {};
  if (config.mode === 'pipeline') {
    const stages = models.stt && models.llm && models.tts ? [models.stt, models.llm, models.tts] : null;
    return `Pipeline · ${(stages ?? ['Deepgram', 'LLM', 'Cartesia']).join(' → ')}`;
  }
  return `Speech-to-speech · ${models.llm ?? REALTIME_MODEL}`;
}
