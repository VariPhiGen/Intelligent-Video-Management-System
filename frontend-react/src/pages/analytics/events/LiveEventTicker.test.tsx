/**
 * LiveEventTicker — at most ten, newest on top, and every row can open playback.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LiveEventTicker } from './LiveEventTicker';
import { aiEvent } from '@/test/aiEvents';

const at = (min: number) => new Date(Date.UTC(2026, 8, 13, 14, min)).toISOString();
const twelve = Array.from({ length: 12 }, (_, i) => aiEvent(`e${i}`, at(i), {
  camera: { id: 'c', slug: `cam${i}-x`, name: `Camera ${i}` },
}));

function renderTicker(props: Partial<Parameters<typeof LiveEventTicker>[0]> = {}) {
  const onOpenPlayback = vi.fn();
  const utils = render(
    <LiveEventTicker events={twelve} fresh={new Set()} canPlayback onOpenPlayback={onOpenPlayback} {...props} />,
  );
  return { ...utils, onOpenPlayback };
}

describe('LiveEventTicker', () => {
  it('shows at most the latest ten events, newest on top', () => {
    const { container } = renderTicker();
    const rows = [...container.querySelectorAll('[data-event-id]')].map(r => r.getAttribute('data-event-id'));
    expect(rows).toEqual(['e11', 'e10', 'e9', 'e8', 'e7', 'e6', 'e5', 'e4', 'e3', 'e2']);
  });

  it('gives each event its camera, activity and time — and no image', () => {
    const { container } = renderTicker({ events: [twelve[3]] });
    const row = screen.getByRole('listitem');
    expect(within(row).getByText('Camera 3')).toBeInTheDocument();
    expect(within(row).getByText('Restricted zone entry')).toBeInTheDocument();
    expect(row.querySelector('.aev-time')?.textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
    expect(container.querySelector('img')).toBeNull();
  });

  it('opens playback for the event on that row', () => {
    const { onOpenPlayback } = renderTicker();
    const row = screen.getAllByRole('listitem')[0];
    fireEvent.click(within(row).getByRole('button', { name: /open playback/i }));
    expect(onOpenPlayback).toHaveBeenCalledWith(twelve[11]);
  });

  it('shows which way an Entry / Exit crossing went, and offers no clip for it', () => {
    const EE = { key: 'entry_exit_WLE_logs', label: 'Entry / exit', color: '#4fd1c5' };
    renderTicker({ events: [
      aiEvent('in', at(3), { activity: EE, zone: 'Door line', attributes: { direction: 'entry' } }),
      aiEvent('out', at(2), { activity: EE, zone: 'Door line', attributes: { direction: 'exit' } }),
      aiEvent('touch', at(1), { activity: EE, zone: 'Door line', attributes: {} }),
    ] });
    const rows = screen.getAllByRole('listitem');
    expect(rows.map(r => r.querySelector('.aev-cross')?.textContent)).toEqual(['Entry', 'Exit', 'No direction']);
    expect(screen.queryByRole('button', { name: /open playback/i })).toBeNull();
  });

  it('offers no playback to a role that cannot use it', () => {
    renderTicker({ canPlayback: false });
    expect(screen.queryByRole('button', { name: /open playback/i })).toBeNull();
  });

  it('marks a newly arrived event so it can animate in', () => {
    const { container } = renderTicker({ fresh: new Set(['e11']) });
    expect(container.querySelector('[data-event-id="e11"]')).toHaveClass('aev-fresh');
    expect(container.querySelector('[data-event-id="e10"]')).not.toHaveClass('aev-fresh');
  });

  it('tells a quiet site apart from an unreachable feed', () => {
    const quiet = renderTicker({ events: [] });
    expect(screen.getByText(/no ai events yet/i)).toBeInTheDocument();
    quiet.unmount();
    renderTicker({ events: [], unavailable: 'HTTP 502' });
    expect(screen.getByText(/unavailable right now/i)).toBeInTheDocument();
    expect(screen.getByText('Reconnecting')).toBeInTheDocument();
  });
});
