import { type UIEvent, useCallback, useRef } from 'react';
import { formatClock } from '../lib/format.ts';
import type { TranscriptEntry } from '../lib/transcript.ts';

interface TranscriptProps {
  entries: readonly TranscriptEntry[];
  emptyHint: string;
  title?: string;
}

/** Follows the newest line unless the reader has scrolled up to look at something. */
export function Transcript({ entries, emptyHint, title = 'Transcript' }: TranscriptProps) {
  const following = useRef(true);

  const onScroll = (event: UIEvent<HTMLOListElement>) => {
    const el = event.currentTarget;
    following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  // Stick to the bottom whenever lines are added or an interim line grows.
  const attachList = useCallback((el: HTMLOListElement | null) => {
    if (!el) return;
    const stick = () => {
      if (following.current) el.scrollTop = el.scrollHeight;
    };
    stick();
    const observer = new MutationObserver(stick);
    observer.observe(el, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, []);

  return (
    <section className="card transcript" aria-labelledby="transcript-title">
      <header className="card__header">
        <h2 id="transcript-title" className="eyebrow">
          {title}
        </h2>
        {entries.length > 0 ? (
          <span className="card__meta">
            {entries.length} {entries.length === 1 ? 'line' : 'lines'}
          </span>
        ) : null}
      </header>
      {entries.length === 0 ? (
        <p className="empty-hint">{emptyHint}</p>
      ) : (
        // Focusable so keyboard users can scroll it; additions are announced, interim edits not.
        <ol
          ref={attachList}
          className="transcript__list"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Call transcript"
          tabIndex={0}
          onScroll={onScroll}
        >
          {entries.map((entry) => (
            <li
              key={entry.id}
              className={`turn turn--${entry.speaker}`}
              data-interim={entry.interim || undefined}
            >
              <div className="turn__meta">
                <span className="turn__who">{entry.label}</span>
                <time className="turn__time" dateTime={new Date(entry.timestamp).toISOString()}>
                  {formatClock(entry.timestamp)}
                </time>
              </div>
              <p className="turn__text">{entry.text}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
