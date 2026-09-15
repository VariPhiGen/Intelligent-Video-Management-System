/**
 * EventsTab — the page as an operator uses it, against a stubbed API.
 *
 * The pieces have their own tests; this one proves they are wired: the ticker
 * and the camera cards come from the overview, Open Playback plays the event's
 * clip in place (never navigating away), and opening a camera sends the camera,
 * activity and date filters the API is asked to apply.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { EventsOverview } from '@/lib/aiEvents';
import { mockApi } from '@/test/api';
import { aiEvent } from '@/test/aiEvents';

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ me: { kind: 'user', roles: ['operator'], permissions: { playback_search: true, ai_analytics: true } } }),
  authHeaders: async () => ({}),
}));

import { EventsTab } from './EventsTab';

const at = (min: number) => new Date(Date.UTC(2026, 8, 13, 14, min)).toISOString();
const RZE = { key: 'restricted_zone_entry', label: 'Restricted zone entry', color: '#ffb020' };
const NPA = { key: 'no_person_area', label: 'No-person area', color: '#4fd1c5' };
const PARK = { key: 'stray_parking', label: 'Stray parking', color: '#52c77e' };

const gateCam = { id: 'g', slug: 'gate-a1b2', name: 'Gate' };
const S12 = Date.parse(at(12)) / 1000;
const newest = aiEvent('ev-new', at(12), { camera: gateCam, activity: NPA,
  playback: { camera: 'gate-a1b2', start: S12 - 10, end: S12 + 20 } });
const older = aiEvent('ev-old', at(3), { camera: gateCam, activity: RZE });

const OVERVIEW: EventsOverview = {
  generated_at: at(13),
  ticker_limit: 10,
  latest: [newest, older],
  cameras: [
    { camera: { ...gateCam, enabled: true }, activities: [RZE, NPA], events: [newest, older], last_event_at: at(12) },
    { camera: { id: 'y', slug: 'yard-c3d4', name: 'Yard', enabled: true }, activities: [PARK], events: [], last_event_at: null },
  ],
};

const OVERVIEW_URL = 'GET /api/analytics/events/overview?per_camera=5';

function Probe() {
  const loc = useLocation();
  return <div data-testid="playback">{loc.pathname + loc.search}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/analytics?tab=events']}>
      <Routes>
        <Route path="/analytics" element={<EventsTab onOpenConfig={() => {}} />} />
        <Route path="/playback" element={<Probe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('EventsTab', () => {
  it('draws the live ticker and a card for every configured camera, quiet ones included', async () => {
    mockApi({ [OVERVIEW_URL]: OVERVIEW });
    renderPage();

    const ticker = await screen.findByRole('list', { name: 'Latest AI events' });
    const rows = within(ticker).getAllByRole('listitem');
    expect(rows.map(r => r.getAttribute('data-event-id'))).toEqual(['ev-new', 'ev-old']);

    const gate = screen.getByRole('region', { name: 'Gate AI events' });
    expect(within(gate).getByText('Restricted zone entry', { selector: '.aev-chip' })).toBeInTheDocument();
    expect(within(gate).getByText('No-person area', { selector: '.aev-chip' })).toBeInTheDocument();

    const yard = screen.getByRole('region', { name: 'Yard AI events' });
    expect(within(yard).getByText('No events recorded')).toBeInTheDocument();
  });

  it('ends with Entry / Exit analytics, for the cameras that have it configured', async () => {
    const EE = { key: 'entry_exit_WLE_logs', label: 'Entry / exit', color: '#4fd1c5' };
    const withDoor: EventsOverview = { ...OVERVIEW, cameras: [...OVERVIEW.cameras,
      { camera: { id: 'd', slug: 'door-e5f6', name: 'Door', enabled: true }, activities: [EE], events: [], last_event_at: null }] };
    const tz = -new Date().getTimezoneOffset() || 0;
    const graph = `GET /api/analytics/events/entry-exit?camera=door-e5f6&minutes=60&tz_offset=${tz}`;
    const api = mockApi({ [OVERVIEW_URL]: withDoor, [graph]: {
      camera: { id: 'd', slug: 'door-e5f6', name: 'Door' }, configured: true, tripwires: [], tripwire: null,
      minutes: 60, bucket_seconds: 120, tz_offset: tz, start: at(0), end: at(60),
      totals: { entry: 4, exit: 2 },
      buckets: [{ start: at(0), entry: 4, exit: 2 }],
    } });
    const { container } = renderPage();

    const section = await screen.findByRole('region', { name: /entry \/ exit analytics/i });
    expect(container.querySelector('.fade')!.lastElementChild).toBe(section);
    await waitFor(() => expect(within(section).getByLabelText('Entry total')).toHaveTextContent('4'));
    expect(within(section).getByLabelText('Exit total')).toHaveTextContent('2');
    expect([...within(section).getByRole('combobox', { name: 'Camera' }).querySelectorAll('option')]
      .map(o => o.textContent)).toEqual(['Door']);
    expect(api.called(graph)).toBe(true);
  });

  it('plays the event clip in place instead of leaving for the Playback page', async () => {
    const createObjectURL = vi.fn(() => 'blob:event-clip');
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const clipKey = `GET /api/nvr/clip?camera=gate-a1b2&timestamp=${encodeURIComponent(newest.started_at)}` +
      '&before=10&after=30';
    try {
      const api = mockApi({ [OVERVIEW_URL]: OVERVIEW, [clipKey]: { status: 200, body: 'mp4' } });
      const { container } = renderPage();
      const ticker = await screen.findByRole('list', { name: 'Latest AI events' });
      fireEvent.click(within(within(ticker).getAllByRole('listitem')[0]).getByRole('button', { name: /open playback/i }));

      const dialog = await screen.findByRole('dialog', { name: 'Event clip' });
      expect(dialog).toHaveTextContent('Gate · No-person area');
      await waitFor(() => expect(container.ownerDocument.querySelector('video')).toHaveAttribute('src', 'blob:event-clip'));
      expect(api.called(clipKey)).toBe(true);
      expect(screen.queryByTestId('playback')).toBeNull();          // still on the Events page

      fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
      expect(screen.queryByRole('dialog')).toBeNull();
    } finally {
      Object.assign(URL, { createObjectURL: original.create, revokeObjectURL: original.revoke });
    }
  });

  it('opens a camera and filters its events by activity and date', async () => {
    const from = new Date('2026-09-13T10:00').toISOString();
    const to = new Date('2026-09-13T18:00').toISOString();
    const api = mockApi({
      [OVERVIEW_URL]: OVERVIEW,
      'GET /api/analytics/events?camera=gate-a1b2&limit=50': { events: [newest, older], next_before: null },
      'GET /api/analytics/events?camera=gate-a1b2&activity=no_person_area&limit=50': { events: [newest], next_before: null },
      [`GET /api/analytics/events?camera=gate-a1b2&activity=no_person_area&from=${encodeURIComponent(from)}&limit=50`]:
        { events: [newest], next_before: null },
      [`GET /api/analytics/events?camera=gate-a1b2&activity=no_person_area&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&limit=50`]:
        { events: [], next_before: null },
    });
    const { container } = renderPage();

    const gate = await screen.findByRole('region', { name: 'Gate AI events' });
    fireEvent.click(within(gate).getByRole('button', { name: /view all/i }));

    await waitFor(() => expect(container.querySelectorAll('tbody [data-event-id]')).toHaveLength(2));
    const activity = screen.getByRole('combobox', { name: 'Activity' });
    expect([...activity.querySelectorAll('option')].map(o => o.textContent))
      .toEqual(['All activities', 'Restricted zone entry', 'No-person area']);

    fireEvent.change(activity, { target: { value: 'no_person_area' } });
    await waitFor(() => expect(container.querySelectorAll('tbody [data-event-id]')).toHaveLength(1));

    fireEvent.change(container.querySelector('input[aria-label="From"]')!, { target: { value: '2026-09-13T10:00' } });
    fireEvent.change(container.querySelector('input[aria-label="To"]')!, { target: { value: '2026-09-13T18:00' } });
    expect(await screen.findByText('No events recorded')).toBeInTheDocument();
    expect(screen.getByText('Nothing matches these filters.')).toBeInTheDocument();
    expect(api.calls.at(-1)).toContain(`to=${encodeURIComponent(to)}`);
  });

  it('refuses a range that ends before it starts without asking the API', async () => {
    const to = new Date('2026-09-13T08:00').toISOString();
    const api = mockApi({
      [OVERVIEW_URL]: OVERVIEW,
      'GET /api/analytics/events?camera=gate-a1b2&limit=50': { events: [], next_before: null },
      [`GET /api/analytics/events?camera=gate-a1b2&to=${encodeURIComponent(to)}&limit=50`]:
        { events: [], next_before: null },
    });
    const { container } = renderPage();
    fireEvent.click(within(await screen.findByRole('region', { name: 'Gate AI events' }))
      .getByRole('button', { name: /view all/i }));
    await screen.findByText('No events recorded');
    const before = api.calls.length;

    fireEvent.change(container.querySelector('input[aria-label="To"]')!, { target: { value: '2026-09-13T08:00' } });
    await waitFor(() => expect(api.calls.length).toBeGreaterThan(before));   // the To-only query is valid
    const settled = api.calls.length;
    fireEvent.change(container.querySelector('input[aria-label="From"]')!, { target: { value: '2026-09-13T09:00' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('"From" is after "To".');
    expect(api.calls.length).toBe(settled);
  });
});
