import { describe, expect, it } from 'vitest';
import { formatDate } from './format';

describe('formatDate', () => {
  it('formats a date as YYYY-MM-DD', () => {
    expect(formatDate(new Date(2026, 9, 20))).toBe('2026-10-20');
  });

  it('zero-pads month and day', () => {
    expect(formatDate(new Date(2024, 0, 5))).toBe('2024-01-05');
  });
});
