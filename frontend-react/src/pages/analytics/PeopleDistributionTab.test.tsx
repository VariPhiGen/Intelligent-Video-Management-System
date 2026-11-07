/**
 * People Distribution — the live view as an operator reads it, against a
 * stubbed API.
 *
 * What it must get right: a zone count reads as people, not detections; a
 * stale count says it is not the present; a camera configured but silent is
 * not drawn as an empty room; and a site with nothing configured is told the
 * one thing to do next.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { localDay, type BucketReading, type DistributionCamera,
  type LiveOverview, type Sample } from '@/lib/peopleDistribution';
import { mockApi } from '@/test/api';

import { PeopleDistributionTab } from './PeopleDistributionTab';

function sample(over: Partial<Sample> = {}): Sample {
  return {
    captured_at: '2026-09-28T10:00:00Z', age_seconds: 4, total_count: 3, source: 'analytics',
    live: true,
    zones: [
      { zone_id: 'z1', zone_name: 'Queue', current_count: 2, dwell_seconds_max: 95, dwell_seconds_avg: 40 },
      { zone_id: 'z0', zone_name: '__unzoned__', current_count: 1, dwell_seconds_max: 0, dwell_seconds_avg: 0 },
    ],
    ...over,
  };
}

function camera(over: Partial<DistributionCamera> = {}): DistributionCamera {
  return {
    camera_id: 'cam-1', slug: 'lobby-a1b2', name: 'Lobby', camera_enabled: true,
    health: 'online', sample: sample(), ...over,
  };
}

function buckets(id: string, day: string, minutes: number): BucketReading {
  return { camera_id: id, name: id, day, minutes, buckets: [
    { start: `${day}T03:30:00Z`, label: '09:00', end_label: '10:00', samples: 360,
      people_avg: 1.5, people_peak: 4, dwell_avg_s: 42, dwell_max_s: 300 },
  ] };
}

function show(...cameras: DistributionCamera[]) {
  const body: LiveOverview = { cameras, stale_after_seconds: 60, engine: 'analytics' };
  const day = localDay();
  const routes: Record<string, unknown> = { 'GET /api/analytics/people-distribution': body };
  for (const c of cameras) {
    const base = `GET /api/analytics/people-distribution/${c.camera_id}`;
    for (const m of [15, 30, 60, 180, 360, 720, 1440]) {
      routes[`${base}/buckets?day=${day}&minutes=${m}`] = buckets(c.camera_id, day, m);
    }
  }
  return mockApi(routes as never);
}

describe('the live view', () => {
  it('counts people per zone, and names the space outside every zone', async () => {
    show(camera());
    render(<PeopleDistributionTab />);
    expect(await screen.findByText('Lobby')).toBeInTheDocument();
    expect(screen.getByText('Queue')).toBeInTheDocument();
    // The engine's sentinel is not a name an operator should ever read.
    expect(screen.getByText('Outside any zone')).toBeInTheDocument();
    expect(screen.queryByText('__unzoned__')).not.toBeInTheDocument();
    expect(screen.getByText(/longest 95s/)).toBeInTheDocument();
  });

  it('says a stale count is the last known one, not the current one', async () => {
    show(camera({ sample: sample({ live: false, age_seconds: 900 }) }));
    render(<PeopleDistributionTab />);
    expect(await screen.findByText(/last known count, not the current one/)).toBeInTheDocument();
  });

  it('does not draw a camera that has not reported yet as an empty room', async () => {
    show(camera({ sample: null }));
    render(<PeopleDistributionTab />);
    expect(await screen.findByText(/no occupancy has been reported yet/)).toBeInTheDocument();
    expect(screen.queryByText('Total')).not.toBeInTheDocument();
  });
});

describe('one view: camera, date, charts', () => {
  it('opens on the first camera alphabetically and on today', async () => {
    show(camera({ camera_id: 'cam-z', name: 'Zeta' }), camera({ camera_id: 'cam-a', name: 'Alpha' }));
    render(<PeopleDistributionTab />);
    const select = await screen.findByLabelText('Camera') as HTMLSelectElement;
    expect(select.value).toBe('cam-a');
    expect((screen.getByLabelText('Date') as HTMLInputElement).value).toBe(localDay());
    expect(screen.getByText('Right now')).toBeInTheDocument();
  });

  it('draws the occupancy and dwell charts, and no distinct-people chart', async () => {
    const api = show(camera());
    render(<PeopleDistributionTab />);
    expect(await screen.findByRole('img', { name: /People in zone on/ })).toBeInTheDocument();
    expect(await screen.findByRole('img', { name: /Dwell time on/ })).toBeInTheDocument();
    // Distinct tracker ids overcount people (a lost-and-found person gets a new
    // id), so the page neither draws that chart nor asks for its data.
    expect(screen.queryByText('Different people per hour')).not.toBeInTheDocument();
    expect(api.called(`GET /api/analytics/people-distribution/cam-1/day?day=${localDay()}`)).toBe(false);
  });

  it('refetches the occupancy chart at the interval picked', async () => {
    const api = show(camera());
    render(<PeopleDistributionTab />);
    await screen.findByRole('img', { name: /People in zone on/ });
    fireEvent.click(screen.getAllByRole('button', { name: '15 min' })[0]);
    const key = `GET /api/analytics/people-distribution/cam-1/buckets?day=${localDay()}&minutes=15`;
    await vi.waitFor(() => expect(api.called(key)).toBe(true));
  });
});

describe('nothing configured', () => {
  it('offers AI Config, the one thing to do next', async () => {
    const onOpenConfig = vi.fn();
    show();
    render(<PeopleDistributionTab onOpenConfig={onOpenConfig} />);
    expect(await screen.findByText('No camera counts people yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open AI Config' }));
    expect(onOpenConfig).toHaveBeenCalled();
  });
});
