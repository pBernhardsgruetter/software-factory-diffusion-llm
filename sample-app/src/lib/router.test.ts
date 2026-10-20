import { describe, expect, it } from 'vitest';
import { parseRoute } from './router';

describe('parseRoute', () => {
  it('maps #/about to about', () => {
    expect(parseRoute('#/about')).toBe('about');
  });

  it('falls back to home', () => {
    expect(parseRoute('')).toBe('home');
    expect(parseRoute('#/nope')).toBe('home');
  });
});
