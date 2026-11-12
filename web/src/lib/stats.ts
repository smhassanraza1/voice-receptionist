/** Linear-interpolated percentile (the same definition as numpy's default). */
export function percentile(values: readonly number[], p: number): number | null {
  if (values.length === 0) return null;
  if (p < 0 || p > 100 || Number.isNaN(p)) throw new RangeError(`percentile ${p} out of range`);
  const sorted = [...values].sort((a, b) => a - b);
  const rank = (p / 100) * (sorted.length - 1);
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  const lo = sorted[lower]!;
  const hi = sorted[upper]!;
  return lo + (hi - lo) * (rank - lower);
}

export const median = (values: readonly number[]): number | null => percentile(values, 50);

/** A round axis maximum at or above `value` (1, 2, 2.5 or 5 times a power of ten). */
export function niceCeiling(value: number): number {
  if (!(value > 0)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * base) return step * base;
  }
  return 10 * base;
}
