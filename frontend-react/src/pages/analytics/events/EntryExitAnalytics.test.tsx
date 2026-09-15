/**
 * EntryExitAnalytics — the graph at the bottom of the Events page, against a
 * stubbed API: only Entry / Exit cameras are offered, the totals are the stored
 * counts the API returns, and every camera, duration and tripwire choice asks
 * the API for exactly that.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  ENTRY_EXIT_ACTIVITY, localUtcOffsetMinutes, type AiEventActivity, type CameraEventsCard, type EntryExitStats,
} from '@/lib/aiEvents';
import { mockApi } from '@/test/api';
import { EntryExitAnalytics } from './EntryExitAnalytics';

const EE: AiEventActivity = { key: ENTRY_EXIT_ACTIVITY, label: 'Entry / exit', color: '#4fd1c5' };
const RZE: AiEventActivity = { key: 'restricted_zone_entry', label: 'Restricted zone entry', color: '#ffb020' };
const TZ = localUtcOffsetMinutes();
const BUCKET: Record<number, number> = { 5: 10, 15: 30, 30: 60, 60: 120, 120: 300, 360: 900, 720: 1800, 1440: 3600 };
const DURATIONS = ['5 min', '15 min', '30 min', '1 h', '2 h', '6 h', '12 h', '24 h'];

const url = (camera: string, minutes: number, tripwire?: string) =>
  `GET /api/analytics/events/entry-exit?camera=${camera}&minutes=${minutes}` +
  `${tripwire ? `&tripwire=${tripwire}` : ''}&tz_offset=${TZ}`;

function card(slug: string, name: string, activities: AiEventActivity[]): CameraEventsCard {
  return { camera: { id: slug, slug, name, enabled: true }, activities, events: [], last_event_at: null };
}

const CARDS = [
  card('door-a1b2', 'Front door', [RZE, EE]),
  card('yard-c3d4', 'Yard', [RZE]),
  card('dock-e5f6', 'Dock', [EE]),
];

const WIRE = { id: 'region_2', name: 'Door line', direction: 'a2b', oriented: true };

/** The API's answer: `counts[i]` = [entries, exits] in bucket i; totals are their sums. */
function stats(camera: string, minutes: number, counts: [number, number][] = [],
               over: Partial<EntryExitStats> = {}): EntryExitStats {
  const bucketS = BUCKET[minutes];
  const start = Date.UTC(2026, 8, 15, 9, 0);
  const buckets = Array.from({ length: (minutes * 60) / bucketS }, (_, i) => ({
    start: new Date(start + i * bucketS * 1000).toISOString(),
    entry: counts[i]?.[0] ?? 0,
    exit: counts[i]?.[1] ?? 0,
  }));
  return {
    camera: { id: camera, slug: camera, name: camera }, configured: true, tripwires: [WIRE], tripwire: null,
    minutes, bucket_seconds: bucketS, tz_offset: TZ,
    start: new Date(start).toISOString(), end: new Date(start + minutes * 60_000).toISOString(),
    totals: {
      entry: buckets.reduce((s, b) => s + b.entry, 0),
      exit: buckets.reduce((s, b) => s + b.exit, 0),
    },
    buckets,
    ...over,
  };
}

const total = (label: 'Entry' | 'Exit') => screen.getByLabelText(`${label} total`).textContent;

describe('EntryExitAnalytics', () => {
  it('offers only cameras with Entry / Exit configured, the first one over the last hour by default', async () => {
    const api = mockApi({ [url('door-a1b2', 60)]: stats('door-a1b2', 60, [[3, 1], [0, 2], [4, 0]]) });
    render(<EntryExitAnalytics cards={CARDS} />);

    await waitFor(() => expect(total('Entry')).toBe('7'));
    expect(total('Exit')).toBe('3');
    expect(api.calls).toEqual([url('door-a1b2', 60)]);

    const camera = screen.getByRole('combobox', { name: 'Camera' });
    expect([...camera.querySelectorAll('option')].map(o => o.textContent)).toEqual(['Front door', 'Dock']);
    const duration = screen.getByRole('group', { name: 'Duration' });
    expect(within(duration).getAllByRole('button').map(b => b.textContent)).toEqual(DURATIONS);
    expect(within(duration).getByRole('button', { name: '1 h' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('draws Entry and Exit as two smooth filled lines in different colours, with a legend', async () => {
    mockApi({ [url('door-a1b2', 60)]: stats('door-a1b2', 60, [[0, 0], [5, 1], [2, 6], [0, 0]]) });
    const { container } = render(<EntryExitAnalytics cards={CARDS} />);
    await waitFor(() => expect(total('Entry')).toBe('7'));

    const line = (s: string) => container.querySelector(`path[data-series="${s}"][data-part="line"]`)!;
    const area = (s: string) => container.querySelector(`path[data-series="${s}"][data-part="area"]`)!;
    for (const s of ['entry', 'exit']) {
      expect(line(s).getAttribute('d')).toMatch(/^M[\d.,-]+C/);         // cubic curves…
      expect(line(s).getAttribute('d')).not.toMatch(/[HV]/);           // …never steps or blocks
      expect(area(s).getAttribute('d')).toMatch(/Z$/);                 // filled down to the baseline
    }
    expect(line('entry').getAttribute('stroke')).not.toBe(line('exit').getAttribute('stroke'));
    expect(screen.getByText('Entry:')).toBeInTheDocument();
    expect(screen.getByText('Exit:')).toBeInTheDocument();
  });

  it('switches camera', async () => {
    const api = mockApi({
      [url('door-a1b2', 60)]: stats('door-a1b2', 60, [[1, 1]]),
      [url('dock-e5f6', 60)]: stats('dock-e5f6', 60, [[9, 4], [2, 0]]),
    });
    render(<EntryExitAnalytics cards={CARDS} />);
    await waitFor(() => expect(total('Entry')).toBe('1'));

    fireEvent.change(screen.getByRole('combobox', { name: 'Camera' }), { target: { value: 'dock-e5f6' } });
    await waitFor(() => expect(total('Entry')).toBe('11'));
    expect(total('Exit')).toBe('4');
    expect(api.called(url('dock-e5f6', 60))).toBe(true);
  });

  it('asks for exactly each duration, from 5 minutes up to 24 hours at most', async () => {
    const routes = Object.fromEntries(Object.keys(BUCKET).map(Number)
      .map(m => [url('door-a1b2', m), stats('door-a1b2', m, [[m, 1]])]));
    const api = mockApi(routes);
    render(<EntryExitAnalytics cards={CARDS} />);
    await waitFor(() => expect(total('Entry')).toBe('60'));

    const duration = screen.getByRole('group', { name: 'Duration' });
    for (const [i, m] of [5, 15, 30, 60, 120, 360, 720, 1440].entries()) {
      fireEvent.click(within(duration).getByRole('button', { name: DURATIONS[i] }));
      await waitFor(() => expect(total('Entry')).toBe(String(m)));
      expect(api.called(url('door-a1b2', m))).toBe(true);
      expect(within(duration).getByRole('button', { name: DURATIONS[i] })).toHaveAttribute('aria-pressed', 'true');
    }
    expect(api.calls.every(c => /minutes=(5|15|30|60|120|360|720|1440)&/.test(c))).toBe(true);
  });

  it('filters by one tripwire when the camera has several, and names a tripwire that is not counted', async () => {
    const wires = [WIRE, { id: 'region_3', name: 'Side gate', direction: 'both', oriented: false }];
    const api = mockApi({
      [url('door-a1b2', 60)]: stats('door-a1b2', 60, [[5, 5]], { tripwires: wires }),
      [url('door-a1b2', 60, 'region_2')]: stats('door-a1b2', 60, [[5, 5]], { tripwires: wires, tripwire: 'region_2' }),
    });
    render(<EntryExitAnalytics cards={CARDS} />);

    const tripwire = await screen.findByRole('combobox', { name: 'Tripwire' });
    expect([...tripwire.querySelectorAll('option')].map(o => o.textContent))
      .toEqual(['All tripwires', 'Door line', 'Side gate']);
    expect(screen.getByRole('note')).toHaveTextContent('Side gate has no entry direction');

    fireEvent.change(tripwire, { target: { value: 'region_2' } });
    await waitFor(() => expect(api.called(url('door-a1b2', 60, 'region_2'))).toBe(true));
    await waitFor(() => expect(total('Entry')).toBe('5'));
  });

  it('offers no tripwire filter for a single tripwire', async () => {
    mockApi({ [url('door-a1b2', 60)]: stats('door-a1b2', 60) });
    render(<EntryExitAnalytics cards={CARDS} />);
    await waitFor(() => expect(total('Entry')).toBe('0'));
    expect(screen.queryByRole('combobox', { name: 'Tripwire' })).toBeNull();
    expect(screen.getByText('No crossings in the last hour.')).toBeInTheDocument();
  });

  it('says so when no camera has Entry / Exit configured, without asking the API', () => {
    const api = mockApi({});
    render(<EntryExitAnalytics cards={[CARDS[1]]} />);
    expect(screen.getByText('No camera has Entry / Exit configured')).toBeInTheDocument();
    expect(api.calls).toEqual([]);
  });
});
