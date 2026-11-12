import { useState } from 'react';
import { latencyP50, type TurnEntry } from '../../lib/events.ts';
import { formatMs } from '../../lib/format.ts';
import { LatencyChart } from './LatencyChart.tsx';

type View = 'chart' | 'table';

/**
 * Reply time: the typical (median) wait from the caller going quiet to the agent's voice, with a
 * per-turn chart and table one click away. `serverTurns`: the realtime model's own voice detection
 * ends turns, so timing starts at its end-of-turn signal.
 */
export function LatencyPanel({ turns, serverTurns = false }: { turns: readonly TurnEntry[]; serverTurns?: boolean }) {
  const [view, setView] = useState<View>('chart');
  // The chart lays out in real pixels, so it mounts only once the disclosure is open.
  const [open, setOpen] = useState(false);
  const p50 = latencyP50(turns);
  const last = turns.at(-1) ?? null;

  return (
    <section className="backstage__section" aria-labelledby="latency-title">
      <h3 id="latency-title" className="eyebrow">
        Reply time
      </h3>
      {turns.length === 0 ? (
        <p className="empty-hint">Measured from the receptionist’s first answer.</p>
      ) : (
        <>
          <div className="reply-time">
            <p className="reply-time__value">{formatMs(p50.e2e)}</p>
            <p className="reply-time__label">
              typical wait over {turns.length} {turns.length === 1 ? 'answer' : 'answers'},{' '}
              {serverTurns ? 'from the model deciding you’d finished' : 'from you going quiet'} to its voice
              {last && last.e2e !== null ? <span> · last one {formatMs(last.e2e)}</span> : null}
            </p>
          </div>
          <details className="reply-details" onToggle={(event) => setOpen(event.currentTarget.open)}>
            <summary className="reply-details__summary">Every answer, step by step</summary>
            {open ? (
              <>
                <fieldset className="toggle-group">
                  <legend className="visually-hidden">Show reply times as</legend>
                  {(['chart', 'table'] as const).map((option) => (
                    <button
                      key={option}
                      type="button"
                      className="toggle-group__item"
                      aria-pressed={view === option}
                      onClick={() => setView(option)}
                    >
                      {option === 'chart' ? 'Chart' : 'Table'}
                    </button>
                  ))}
                </fieldset>
                {view === 'chart' ? (
                  <LatencyChart turns={turns} p50={p50.e2e} />
                ) : (
                  <section className="table-scroll" tabIndex={0} aria-label="Reply time per answer">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th scope="col">Answer</th>
                          <th scope="col">Total</th>
                          <th scope="col">Noticing you’d finished</th>
                          <th scope="col">First words</th>
                          <th scope="col">Voice starts</th>
                        </tr>
                      </thead>
                      <tbody>
                        {[...turns].reverse().map((t) => (
                          <tr key={t.turn}>
                            <th scope="row">{t.turn}</th>
                            <td>{formatMs(t.e2e)}</td>
                            <td>{formatMs(t.eou)}</td>
                            <td>{formatMs(t.llmTtft)}</td>
                            <td>{formatMs(t.ttsTtfb)}</td>
                          </tr>
                        ))}
                      </tbody>
                      <tfoot>
                        <tr>
                          <th scope="row">Typical</th>
                          <td>{formatMs(p50.e2e)}</td>
                          <td>{formatMs(p50.eou)}</td>
                          <td>{formatMs(p50.llmTtft)}</td>
                          <td>{formatMs(p50.ttsTtfb)}</td>
                        </tr>
                      </tfoot>
                    </table>
                  </section>
                )}
              </>
            ) : null}
          </details>
        </>
      )}
    </section>
  );
}
