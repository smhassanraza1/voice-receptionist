import { describe, expect, it } from 'vitest';
import { median, niceCeiling, percentile } from './stats.ts';

describe('percentile', () => {
  it('interpolates like numpy', () => {
    expect(percentile([1, 2, 3, 4], 50)).toBe(2.5);
    expect(percentile([10, 20, 30, 40, 50], 90)).toBe(46);
    expect(percentile([7], 99)).toBe(7);
  });

  it('is order-independent and does not mutate its input', () => {
    const values = [900, 300, 600];
    expect(median(values)).toBe(600);
    expect(values).toEqual([900, 300, 600]);
  });

  it('returns null for no data and rejects bad percentiles', () => {
    expect(median([])).toBeNull();
    expect(() => percentile([1], 101)).toThrow(RangeError);
  });
});

describe('niceCeiling', () => {
  it.each([
    [0, 1],
    [180, 200],
    [250, 250],
    [820, 1000],
    [1240, 2000],
    [2600, 5000],
  ])('%d -> %d', (value, expected) => {
    expect(niceCeiling(value)).toBe(expected);
  });
});
