import { type PointerEvent, useState } from 'react';
import { useElementWidth } from '../../hooks/useElementWidth.ts';
import type { TurnEntry } from '../../lib/events.ts';
import { formatMs } from '../../lib/format.ts';
import { niceCeiling } from '../../lib/stats.ts';

const HEIGHT = 156;
const PAD = { top: 16, right: 10, bottom: 24, left: 44 };
const MIN_SLOTS = 10; // keep bars slim while only a few turns exist
const MAX_BARS = 24;
const BAR_MAX_WIDTH = 22;

interface LatencyChartProps {
  turns: readonly TurnEntry[];
  p50: number | null;
}

/** Column with a 4px rounded data end and a square foot on the baseline. */
function columnPath(x: number, top: number, width: number, base: number): string {
  const r = Math.min(4, width / 2, base - top);
  return [
    `M${x},${base}`,
    `V${top + r}`,
    `Q${x},${top} ${x + r},${top}`,
    `H${x + width - r}`,
    `Q${x + width},${top} ${x + width},${top + r}`,
    `V${base}`,
    'Z',
  ].join(' ');
}

/** Tick text in ms below a second, seconds above; only the top tick carries the unit. */
function tickLabel(value: number, max: number): string {
  const unit = value === max ? (max >= 1000 ? ' s' : ' ms') : '';
  if (value === 0) return '0';
  if (max < 1000) return `${value}${unit}`;
  const seconds = value / 1000;
  return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)}${unit}`;
}

/**
 * Reply time per answer, newest on the right, with the running median ("typical") as a reference
 * line. Hover, or focus and use the arrow keys, to select an answer; the readout shows its steps.
 */
export function LatencyChart({ turns, p50 }: LatencyChartProps) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);

  const shown = turns.slice(-MAX_BARS);
  const values = shown.flatMap((t) => (t.e2e === null ? [] : [t.e2e]));
  const yMax = niceCeiling(Math.max(250, p50 ?? 0, ...values));
  const plotWidth = Math.max(0, width - PAD.left - PAD.right);
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;
  const slot = plotWidth / Math.max(MIN_SLOTS, shown.length);
  const barWidth = Math.max(4, Math.min(BAR_MAX_WIDTH, slot - 2 * 2)); // ≥2px surface gap
  const base = PAD.top + plotHeight;
  const y = (v: number) => base - (Math.min(v, yMax) / yMax) * plotHeight;
  const ticks = [0, yMax / 2, yMax];

  const activeTurn = active === null ? null : (shown[active] ?? null);
  const barX = (i: number) => PAD.left + i * slot + (slot - barWidth) / 2;

  const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const index = Math.floor((event.clientX - rect.left - PAD.left) / slot);
    setActive(index >= 0 && index < shown.length ? index : null);
  };

  const readoutLeft =
    active === null ? 0 : Math.min(Math.max(barX(active) + barWidth / 2, 84), width - 84);
  const readoutTop = activeTurn && activeTurn.e2e !== null ? y(activeTurn.e2e) : base;

  // The selected turn, spoken as one sentence for the slider's value text.
  const focusTurn = activeTurn ?? shown.at(-1) ?? null;
  const valueText = focusTurn
    ? `Answer ${focusTurn.turn}: ${formatMs(focusTurn.e2e)} in total; noticing you'd finished ${formatMs(focusTurn.eou)}, first words ${formatMs(focusTurn.llmTtft)}, voice starts ${formatMs(focusTurn.ttsTtfb)}`
    : 'No answers yet';

  return (
    <div ref={wrapRef} className="latency-chart">
      <div className="chart-key" aria-hidden="true">
        <span className="chart-key__item">
          <span className="chart-key__swatch chart-key__swatch--bar" /> Each answer
        </span>
        {p50 !== null ? (
          <span className="chart-key__item">
            <span className="chart-key__swatch chart-key__swatch--line" /> Typical {formatMs(p50)}
          </span>
        ) : null}
      </div>
      {/* A native range input is the keyboard and screen-reader way in: arrows/Home/End move
          between turns and the value text reads that turn's breakdown. Pointers use the SVG. */}
      <input
        type="range"
        className="latency-chart__scrubber"
        min={0}
        max={Math.max(0, shown.length - 1)}
        step={1}
        value={active ?? Math.max(0, shown.length - 1)}
        aria-label="Reply time by answer"
        aria-valuetext={valueText}
        onChange={(event) => setActive(Number(event.target.value))}
        onFocus={() => setActive((a) => a ?? (shown.length ? shown.length - 1 : null))}
        onBlur={() => setActive(null)}
      />
      <svg
        width={width}
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        aria-hidden="true"
        onPointerMove={onPointerMove}
        onPointerLeave={() => setActive(null)}
      >
        {ticks.map((tick) => (
          <g key={tick} className="latency-chart__tick">
            <line x1={PAD.left} x2={width - PAD.right} y1={y(tick)} y2={y(tick)} />
            <text x={PAD.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">
              {tickLabel(tick, yMax)}
            </text>
          </g>
        ))}

        {shown.map((turn, i) =>
          turn.e2e === null ? null : (
            <path
              key={turn.turn}
              className="latency-chart__bar"
              data-active={active === i || undefined}
              data-dim={(active !== null && active !== i) || undefined}
              d={columnPath(barX(i), y(turn.e2e), barWidth, base)}
            />
          ),
        )}

        <line className="latency-chart__baseline" x1={PAD.left} x2={width - PAD.right} y1={base} y2={base} />

        {p50 !== null ? (
          <line
            className="latency-chart__p50"
            x1={PAD.left}
            x2={width - PAD.right}
            y1={y(p50)}
            y2={y(p50)}
          />
        ) : null}

        {shown.length > 0 ? (
          <g className="latency-chart__xlabels">
            <text x={barX(0) + barWidth / 2} y={HEIGHT - 6} textAnchor="middle">
              #{shown[0]!.turn}
            </text>
            {shown.length > 1 ? (
              <text x={barX(shown.length - 1) + barWidth / 2} y={HEIGHT - 6} textAnchor="middle">
                #{shown.at(-1)!.turn}
              </text>
            ) : null}
          </g>
        ) : null}
      </svg>

      {activeTurn ? (
        <div className="chart-readout" aria-hidden="true" style={{ left: readoutLeft, top: readoutTop }}>
          <p className="chart-readout__value">
            {formatMs(activeTurn.e2e)} <span>in total</span>
          </p>
          <p className="chart-readout__label">Answer {activeTurn.turn}</p>
          <dl className="chart-readout__rows">
            <dt>Noticing you’d finished</dt>
            <dd>{formatMs(activeTurn.eou)}</dd>
            <dt>First words</dt>
            <dd>{formatMs(activeTurn.llmTtft)}</dd>
            <dt>Voice starts</dt>
            <dd>{formatMs(activeTurn.ttsTtfb)}</dd>
          </dl>
        </div>
      ) : null}
    </div>
  );
}
