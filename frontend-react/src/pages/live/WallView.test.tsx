import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { Camera } from '@/lib/types';
import { WallView } from './WallView';
import { WALL_PREFS_DEFAULT } from './wallPrefs';

vi.mock('./LiveVideo', () => ({
  LiveVideo: ({ slug }: { slug: string }) => <div data-testid="feed" data-slug={slug} />,
  LvBadges: () => null, Clock: () => null,
}));
vi.mock('@/lib/ptz', () => ({ usePtzPositions: () => ({}) }));

const cams = ['a', 'b', 'c'].map(id => ({ id, slug: id, name: `Camera ${id}`, enabled: true })) as Camera[];
const props = {
  cams, prefs: { ...WALL_PREFS_DEFAULT, hideOffline: false }, theme: 'dark' as const,
  customised: false, title: 'Wall', logoUrl: null,
  onRemove: vi.fn(), onClear: vi.fn(), onPick: vi.fn(), onSettings: vi.fn(),
};
const feeds = () => screen.getAllByTestId('feed').map(el => el.getAttribute('data-slug'));
beforeEach(() => localStorage.clear());

it('returns from a single camera to the complete wall with Show all', () => {
  render(<WallView {...props} />);
  fireEvent.click(screen.getAllByTestId('feed')[0]);
  expect(feeds()).toEqual(['a']);
  fireEvent.click(screen.getByRole('button', { name: 'Show all' }));
  expect(feeds()).toEqual(['a', 'b', 'c']);
});

it('reorders tiles by dragging, persists after remount, and resets the order', () => {
  const { unmount } = render(<WallView {...props} />);
  const data = new Map<string, string>();
  const dataTransfer = { setData: (k: string, v: string) => data.set(k, v), getData: (k: string) => data.get(k) || '' };
  const tiles = screen.getAllByTestId('feed').map(el => el.closest('.wall-cell')!);
  fireEvent.dragStart(tiles[0], { dataTransfer });
  fireEvent.dragOver(tiles[2], { dataTransfer });
  fireEvent.drop(tiles[2], { dataTransfer });
  expect(feeds()).toEqual(['b', 'c', 'a']);
  expect(screen.queryByRole('button', { name: 'Show all' })).not.toBeInTheDocument();
  unmount();
  render(<WallView {...props} />);
  expect(feeds()).toEqual(['b', 'c', 'a']);
  fireEvent.click(screen.getByRole('button', { name: 'Reset order' }));
  expect(feeds()).toEqual(['a', 'b', 'c']);
});

it('supports keyboard rearrangement and appends newly added cameras', () => {
  const { rerender } = render(<WallView {...props} />);
  fireEvent.keyDown(screen.getByRole('button', { name: 'Reorder Camera b' }), { key: 'ArrowLeft' });
  expect(feeds()).toEqual(['b', 'a', 'c']);
  rerender(<WallView {...props} cams={[...cams, { ...cams[0], id: 'd', slug: 'd' }]} />);
  expect(feeds()).toEqual(['b', 'a', 'c', 'd']);
});
