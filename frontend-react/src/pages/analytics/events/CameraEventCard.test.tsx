/**
 * CameraEventCard — every configured activity is named, a quiet camera says
 * so instead of vanishing, and its events read newest first.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { CameraEventsCard } from '@/lib/aiEvents';
import { CameraEventCard } from './CameraEventCard';
import { aiEvent } from '@/test/aiEvents';

const at = (min: number) => new Date(Date.UTC(2026, 8, 13, 14, min)).toISOString();

const ACTIVITIES = [
  { key: 'restricted_zone_entry', label: 'Restricted zone entry', color: '#ffb020' },
  { key: 'no_person_area', label: 'No-person area', color: '#4fd1c5' },
  { key: 'unauthorised_access', label: 'Unauthorised access', color: '#8a94a6' },
];

function card(over: Partial<CameraEventsCard> = {}): CameraEventsCard {
  return {
    camera: { id: 'c1', slug: 'cam2-o8iu', name: 'cam2', enabled: true },
    activities: ACTIVITIES,
    events: [],
    last_event_at: null,
    ...over,
  };
}

function renderCard(c: CameraEventsCard, canPlayback = true) {
  const onOpen = vi.fn();
  const onOpenPlayback = vi.fn();
  const utils = render(<CameraEventCard card={c} fresh={new Set()} canPlayback={canPlayback}
                                        onOpen={onOpen} onOpenPlayback={onOpenPlayback} />);
  return { ...utils, onOpen, onOpenPlayback };
}

describe('CameraEventCard', () => {
  it('names every activity the camera is configured for', () => {
    renderCard(card());
    for (const a of ACTIVITIES) expect(screen.getByText(a.label)).toBeInTheDocument();
  });

  it('keeps a configured camera with no events, and says so', () => {
    renderCard(card());
    expect(screen.getByRole('region', { name: 'cam2 AI events' })).toBeInTheDocument();
    expect(screen.getByText('No events recorded')).toBeInTheDocument();
  });

  it('lists events from all of its activities, newest first', () => {
    const older = aiEvent('old', at(1), { activity: ACTIVITIES[0] });
    const newer = aiEvent('new', at(9), { activity: ACTIVITIES[1] });
    const { container } = renderCard(card({ events: [older, newer] }));
    const ids = [...container.querySelectorAll('[data-event-id]')].map(r => r.getAttribute('data-event-id'));
    expect(ids).toEqual(['new', 'old']);
    const rows = screen.getAllByRole('listitem');
    expect(within(rows[0]).getByText('No-person area')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Restricted zone entry')).toBeInTheDocument();
    expect(screen.queryByText('No events recorded')).toBeNull();
  });

  it('shows an Entry / Exit crossing as Entry or Exit rather than a clip', () => {
    const EE = { key: 'entry_exit_WLE_logs', label: 'Entry / exit', color: '#4fd1c5' };
    const rze = aiEvent('rze', at(1), { activity: ACTIVITIES[0] });
    const out = aiEvent('out', at(5), { activity: EE, attributes: { direction: 'exit' } });
    renderCard(card({ activities: [ACTIVITIES[0], EE], events: [rze, out] }));
    const [first, second] = screen.getAllByRole('listitem');
    expect(within(first).getByText('Exit')).toBeInTheDocument();
    expect(within(first).queryByRole('button', { name: /open playback/i })).toBeNull();
    expect(within(second).getByRole('button', { name: /open playback/i })).toBeInTheDocument();
  });

  it('opens playback for a row, and the full view from the card', () => {
    const ev = aiEvent('only', at(4));
    const { onOpen, onOpenPlayback } = renderCard(card({ events: [ev] }));
    fireEvent.click(screen.getByRole('button', { name: /open playback/i }));
    expect(onOpenPlayback).toHaveBeenCalledWith(ev);
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /view all/i }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
