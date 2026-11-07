import { describe, expect, it } from 'vitest';
import { bestWallGrid } from './wallLayout';

describe('bestWallGrid', () => {
  it('one camera fills the stage', () => {
    const g = bestWallGrid(1, 1920, 1000);
    expect([g.cols, g.rows]).toEqual([1, 1]);
    expect(g.tileH).toBeGreaterThan(950);
  });
  it('fits 9 as 3x3 on a 16:9 stage', () => {
    expect(bestWallGrid(9, 1920, 1080, 0)).toMatchObject({ cols: 3, rows: 3 });
  });
  it('18 cameras on a wide stage use more columns than rows and fit the height', () => {
    const g = bestWallGrid(18, 1900, 900);
    expect(g.cols * g.rows).toBeGreaterThanOrEqual(18);
    expect(g.cols).toBeGreaterThan(g.rows);
    expect(g.tileH * g.rows + 6 * (g.rows - 1)).toBeLessThanOrEqual(900);
    expect(g.tileW * g.cols + 6 * (g.cols - 1)).toBeLessThanOrEqual(1900);
  });
  it('a tall narrow stage stacks', () => {
    expect(bestWallGrid(4, 400, 1600).cols).toBe(1);
  });
  it('every count from 1 to 40 fits and never leaves a whole empty row', () => {
    for (let n = 1; n <= 40; n++) {
      const g = bestWallGrid(n, 1600, 900);
      expect(g.cols * g.rows).toBeGreaterThanOrEqual(n);
      expect(g.cols * (g.rows - 1)).toBeLessThan(n);
    }
  });
});
