/**
 * The live grid pages. This file exists because the version before it did not,
 * and the failure was silent in both directions.
 *
 * WHAT WAS BROKEN. A fixed layout rendered `zoneCams.slice(0, +layout)`. On a
 * hundred-camera site "3×3" showed cameras 1-9 and the other ninety-one were
 * unreachable from this page — no pager, no count, nothing on screen saying a
 * slice had been taken. An operator could only conclude the cameras were gone.
 * Auto had the opposite failure: it rendered EVERY camera, so a hundred HLS
 * sessions were attached at 250 ms intervals, a twenty-five second ramp onto a
 * relay already serving a hundred streams. One bug hid cameras; the other tried
 * to show all of them at once. Paging is the same fix for both.
 *
 * So these assert the two halves that must stay true together: what is on
 * screen, and what the page says is on screen. A pager that disagrees with the
 * grid is worse than no pager, because it is trusted.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const cameras = Array.from({ length: 103 }, (_, i) => ({
  id: `id-${i}`, slug: `cam-${i}`, name: `Camera ${i}`,
  enabled: true, health_status: 'connected', sub_track: null,
  camera_metadata: {},
})) as any[];

vi.mock('@/lib/cameras', () => ({
  useCameras: () => ({ cameras, uptime: {}, loading: false }),
  zoneOf: () => 'Unzoned',
}));
vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));
vi.mock('./LiveVideo', () => ({
  LiveVideo: ({ slug }: { slug: string }) => <div data-testid="tile" data-slug={slug} />,
  LvBadges: () => null,
  Clock: () => null,
}));
vi.mock('./ExpandedView', () => ({ ExpandedView: () => null }));

import { LivePage, liveCols, pageSize } from './LivePage';

const slugs = () => screen.getAllByTestId('tile').map(t => t.getAttribute('data-slug'));

beforeEach(() => localStorage.clear());

it('returns to the camera grid using Show all after expanding a tile', () => {
  render(<LivePage />);
  const before = slugs();
  fireEvent.click(screen.getAllByTestId('tile')[0]);
  expect(screen.queryByTestId('tile')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
  expect(slugs()).toEqual(before);
});

describe('a page is the layout AND the decode budget', () => {
  it('gives a fixed layout exactly its own tile count', () => {
    expect(pageSize('4', 2)).toBe(4);
    expect(pageSize('9', 3)).toBe(9);
    expect(pageSize('16', 4)).toBe(16);
  });

  it('fills whole rows in auto, never a ragged last row', () => {
    // A gap at the end of a paged grid reads as cameras that failed to load.
    for (const cols of [1, 2, 3, 4, 5, 6]) {
      expect(pageSize('auto', cols) % cols).toBe(0);
    }
  });

  it('never exceeds the largest explicit layout in auto', () => {
    // Auto must not quietly attach more streams than 4×4, the size an operator
    // can ask for on purpose.
    for (const cols of [1, 2, 3, 4, 5, 6]) {
      expect(pageSize('auto', cols)).toBeLessThanOrEqual(16);
    }
  });
});

describe('auto columns fit the screen they are on', () => {
  it('puts a seven-camera fleet in one column on a phone', () => {
    // It asked for three by count, and each tile came out ~100px wide with the
    // LIVE/REC badges printed over the camera name.
    expect(liveCols('auto', 7, 330)).toBe(1);
  });

  it('uses two columns on a tablet-width grid', () => {
    expect(liveCols('auto', 7, 700)).toBe(2);
  });

  it('leaves the desktop grid as the camera count decides', () => {
    expect(liveCols('auto', 7, 880)).toBe(3);
    expect(liveCols('auto', 7, 0)).toBe(3);        // not measured yet
  });

  it('never overrides a layout the operator chose', () => {
    expect(liveCols('9', 7, 330)).toBe(3);
  });
});

describe('the grid shows a page, and says which page', () => {
  it('renders one page of tiles, not the whole fleet', () => {
    render(<LivePage />);
    // 103 cameras, and nothing like 103 video elements attached.
    expect(screen.getAllByTestId('tile').length).toBeLessThanOrEqual(16);
  });

  it('says where in the fleet the page sits', () => {
    render(<LivePage />);
    // The range and the total, so "cameras 10-18 of 103" is answerable at a
    // glance. This is the sentence whose absence made the old slice silent.
    expect(screen.getByText(/of 103$/)).toBeInTheDocument();
  });

  it('reaches the cameras a slice used to hide', () => {
    render(<LivePage />);
    const first = slugs();
    fireEvent.click(screen.getByLabelText('Next cameras'));
    const second = slugs();
    expect(second[0]).not.toBe(first[0]);
    expect(second.some(s => first.includes(s!))).toBe(false);
  });

  it('cannot page off either end', () => {
    render(<LivePage />);
    expect(screen.getByLabelText('Previous cameras')).toBeDisabled();
    expect(screen.getByLabelText('Next cameras')).not.toBeDisabled();
  });

  it('starts the counter at the first camera, not at zero', () => {
    // Monitors, pages and camera positions are all one-based for the operator.
    render(<LivePage />);
    expect(screen.getByText(/^1–/)).toBeInTheDocument();
  });
});

// The paged-wall suite ('the wall costs the same whatever the fleet costs') was removed in the
// field -> ai merge: the paged 9-monitor wall was replaced by the auto-layout wall,
// covered by wallLayout.test.ts and wallPrefs.test.ts.
