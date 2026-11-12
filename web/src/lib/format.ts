const msFormat = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const secFormat = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** 820 -> "820 ms", 1240 -> "1.24 s". */
export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return value < 1000 ? `${msFormat.format(value)} ms` : `${secFormat.format(value / 1000)} s`;
}

/** Elapsed call time: 0:07, 3:42, 1:02:09. */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const seconds = String(s % 60).padStart(2, '0');
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}`
    : `${minutes}:${seconds}`;
}

export function formatClock(timestamp: number | string | Date): string {
  return new Date(timestamp).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

const relative = new Intl.RelativeTimeFormat('en', { numeric: 'auto', style: 'short' });

/** "now", "3 min. ago", "2 hr. ago". */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  if (Math.abs(seconds) < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return relative.format(minutes, 'minute');
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return relative.format(hours, 'hour');
  return relative.format(Math.round(hours / 24), 'day');
}

const ACRONYMS = new Set(['llm', 'stt', 'tts', 'eou', 'ttft', 'ttfb', 'id', 'mcp', 'sip', 'kb']);

/** "find_available_slots" -> "Find available slots"; "llm_prompt_tokens" -> "LLM prompt tokens". */
export function humanizeIdentifier(name: string): string {
  const words = name.replace(/[_.-]+/g, ' ').trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return name;
  return words
    .map((word, i) => {
      if (ACRONYMS.has(word.toLowerCase())) return word.toUpperCase();
      return i === 0 ? word[0]!.toUpperCase() + word.slice(1) : word;
    })
    .join(' ');
}
