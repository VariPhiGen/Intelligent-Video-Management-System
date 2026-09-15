/**
 * The NOC video wall is capped at nine monitors, and the cap has to stay
 * consistent in every place it is written down.
 *
 * THE CAP IS NOT A DEFECT — it is a recorded decision, and the reasoning is
 * worth keeping in view because it is the reason these tests pin the limit
 * rather than push against it. The real ceiling is DECODE, not layout:
 * `hlsSrc()` serves one rendition per camera, so every tile decodes the
 * camera's main stream no matter how small it is painted. On a 3x3 at 1600px
 * each cell paints ~500px wide while decoding 1080p or 1440p. Raising the tile
 * count alone ships a wall that looks right and stutters — and it degrades
 * exactly when something is happening, which is the worst NOC failure mode.
 *
 * WHAT ACTUALLY GOES WRONG, and what these tests catch. The number nine is
 * pinned in eight places across two files: `Array(9)` three times,
 * `zoneCams.slice(0, 9)`, `repeat(3, 1fr)`, the `{used} / 9` counter, and two
 * "3x3 monitor array" strings. Nothing ties them together. The realistic
 * regression is not someone deliberately lifting the cap — it is a PARTIAL
 * lift, where one of the eight moves and the rest do not, and the wall ends up
 * claiming one thing while doing another.
 *
 * So these assert agreement: what the wall renders, what it says it is
 * rendering, and what survives a round trip through localStorage.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// LiveVideo opens HLS and starts timers; the wall's geometry and counters are
// what is under test, not video playback.
vi.mock('./LiveVideo', () => ({
  LiveVideo: ({ slug }: { slug: string }) => <div data-testid="live-video" data-slug={slug} />,
  LvBadges: () => null,
  Clock: () => null,
}));

import { WallView } from './WallView';
import { loadWall } from './LivePage';

const WALL_SIZE = 9;

const camera = (n: number) =>
  ({ id: `id-${n}`, slug: `cam-${n}`, name: `Camera ${n}`, sub_track: null }) as any;

/** `cells` as the wall always receives it: a fixed-length array with holes. */
function cells(filled: number, length = WALL_SIZE) {
  return Array.from({ length }, (_, i) => (i < filled ? camera(i) : null));
}

function renderWall(cs: any[]) {
  return render(
    <WallView cells={cs} onCellClick={() => {}} onClear={() => {}} onPushFromLive={() => {}} />,
  );
}

describe('the wall renders exactly the monitors it claims to', () => {
  it('shows nine monitors when the wall is empty', () => {
    renderWall(cells(0));
    // Empty monitors are labelled MONITOR 1..9 — the operator's own numbering.
    expect(screen.getAllByText(/^MONITOR \d+$/)).toHaveLength(WALL_SIZE);
    expect(screen.getByText(`0 / ${WALL_SIZE} monitors active`)).toBeInTheDocument();
  });

  it('counts only the filled monitors, and against the real ceiling', () => {
    renderWall(cells(4));
    expect(screen.getAllByTestId('live-video')).toHaveLength(4);
    expect(screen.getAllByText(/^MONITOR \d+$/)).toHaveLength(WALL_SIZE - 4);
    expect(screen.getByText(`4 / ${WALL_SIZE} monitors active`)).toBeInTheDocument();
  });

  it('is full at nine, and says so', () => {
    renderWall(cells(WALL_SIZE));
    expect(screen.getAllByTestId('live-video')).toHaveLength(WALL_SIZE);
    expect(screen.queryByText(/^MONITOR \d+$/)).not.toBeInTheDocument();
    expect(screen.getByText(`${WALL_SIZE} / ${WALL_SIZE} monitors active`)).toBeInTheDocument();
  });

  it('lays the monitors out as the 3x3 the header advertises', () => {
    // The grid column count and the advertised geometry are two of the eight
    // places the cap is written down. A lift that moves one and not the other
    // is the silent invalid state this whole file exists for.
    const { container } = renderWall(cells(WALL_SIZE));
    const grid = container.querySelector('.wall-stage > div') as HTMLElement;
    expect(grid.style.gridTemplateColumns).toBe('repeat(3, 1fr)');
    expect(screen.getByText(/3×3 monitor array/)).toBeInTheDocument();
    // 3 columns x 3 rows is nine, and nine is what the counter promises.
    expect(screen.getByText(`${WALL_SIZE} / ${WALL_SIZE} monitors active`)).toBeInTheDocument();
  });
});

describe('the persisted wall cannot arrive at the wrong size', () => {
  // THE GOTCHA THE CAP ACTUALLY HAS. The wall persists to localStorage under
  // `vms-wall` as a fixed nine-element array. A workstation that saved a wall
  // under one size and loads it under another is the one way a real deployment
  // reaches an invalid grid without anyone editing code — so the normalisation
  // is load-bearing and is pinned here.
  const KEY = 'vms-wall';

  it('pads a short saved wall up to nine', () => {
    localStorage.setItem(KEY, JSON.stringify(['a', 'b']));
    const out = loadWall();
    expect(out).toHaveLength(WALL_SIZE);
    expect(out.slice(0, 2)).toEqual(['a', 'b']);
    expect(out.slice(2).every(c => c === null)).toBe(true);
  });

  it('truncates a saved wall that is too long', () => {
    localStorage.setItem(KEY, JSON.stringify(Array.from({ length: 16 }, (_, i) => `c${i}`)));
    expect(loadWall()).toHaveLength(WALL_SIZE);
  });

  it('starts fresh on corrupt or absent state rather than throwing', () => {
    localStorage.setItem(KEY, 'not json at all');
    expect(loadWall()).toEqual(Array(WALL_SIZE).fill(null));
    localStorage.removeItem(KEY);
    expect(loadWall()).toEqual(Array(WALL_SIZE).fill(null));
  });

  it('drops non-string entries instead of trusting them into the grid', () => {
    localStorage.setItem(KEY, JSON.stringify([1, { id: 'x' }, 'ok', null]));
    const out = loadWall();
    expect(out).toHaveLength(WALL_SIZE);
    expect(out[0]).toBeNull();
    expect(out[1]).toBeNull();
    expect(out[2]).toBe('ok');
  });
});
