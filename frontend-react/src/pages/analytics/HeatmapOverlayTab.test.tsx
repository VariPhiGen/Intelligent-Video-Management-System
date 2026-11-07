/**
 * Heatmap Overlay — the page as an operator uses it, against a stubbed API.
 *
 * What it must get right: the page READS, it never drives the analytics;
 * turning a camera off warns first and says the schedule is untouched; turning
 * one back on asks whether to resume or start a new session; and a camera with
 * nothing yet still says something useful.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { HeatmapCamera, HeatmapOverview } from '@/lib/heatmaps';
import { mockApi } from '@/test/api';

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ me: { kind: 'user', roles: ['supervisor'],
                          permissions: { ai_analytics: true, camera_manage: true } } }),
  authHeaders: async () => ({}),
}));

import { HeatmapOverlayTab } from './HeatmapOverlayTab';
import { startOfToday } from '@/lib/heatmaps';

// A fixed clock, so the range the page asks for has a known URL.
const NOW = new Date('2026-09-20T15:00:00Z');
// jsdom has no object URLs; the page turns each fetched picture into one.
// Installed for the whole file: cards still unmount after each test's cleanup.
Object.assign(URL, { createObjectURL: () => 'blob:heatmap', revokeObjectURL: () => {} });
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
});
afterEach(() => { vi.useRealTimers(); });

function rangeKeys(cameraId: string, start: Date = startOfToday(NOW), end: Date = NOW) {
  const q = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() }).toString();
  return { summary: `GET /api/analytics/heatmaps/${cameraId}/range?${q}`,
           image: `GET /api/analytics/heatmaps/${cameraId}/range/image?${q}` };
}

function rangeReply(cameraId: string, over: Record<string, unknown> = {}) {
  return { camera_id: cameraId, name: cameraId, start: '', end: '', blocks: 12,
           includes_live: true, points: 3456, covered_from: START, covered_to: UPDATED,
           background_at: 'live', ...over };
}

const START = '2026-09-20T08:00:00Z';
const UPDATED = '2026-09-20T14:32:00Z';
const RUNTIME = 'POST /api/analytics/heatmaps/cam-1/runtime';

function camera(over: Partial<HeatmapCamera> = {}): HeatmapCamera {
  return {
    camera_id: 'cam-1', slug: 'gate-a1b2', name: 'Camera 01', camera_enabled: true,
    health: 'online', runtime_enabled: true,
    schedule: { active_days: ['Mon', 'Tue'], active_hours: { start: '08:00', end: '18:00' } },
    session: { id: 'sess-1', status: 'active', started_at: START, ended_at: null,
               close_reason: null, points: 4200, frames: 9000, params: {} },
    latest_artifact: { id: 77, session_id: 'sess-1', kind: 'snapshot', captured_at: UPDATED,
                       period_start: START, width: 1280, height: 720 },
    ...over,
  };
}

function overview(...cameras: HeatmapCamera[]): HeatmapOverview {
  return { cameras, snapshot_interval_s: 60 };
}

// Heat on 18 and 20 Sep, nothing on 19 Sep.
const AVAILABILITY = { camera_id: 'cam-1', days: ['2026-09-18', '2026-09-20'],
                       first_at: '2026-09-18T06:00:00Z', last_at: UPDATED, blocks: 400,
                       used_bytes: 600 * 1024, retention_days: 30, storage_mb: 500 };

const IMAGE = { status: 200, body: new Blob([new Uint8Array([1, 2, 3])], { type: 'image/jpeg' }) };

function show(body: HeatmapOverview, extra: Record<string, unknown> = {}) {
  const ranges: Record<string, unknown> = {};
  for (const c of body.cameras) {
    const k = rangeKeys(c.camera_id);
    ranges[k.summary] = rangeReply(c.camera_id);
    ranges[k.image] = IMAGE;
    ranges[`GET /api/analytics/heatmaps/${c.camera_id}/availability`] = AVAILABILITY;
  }
  const api = mockApi({
    'GET /api/analytics/heatmaps': body,
    ...ranges,
    ...extra,
  });
  render(<HeatmapOverlayTab />);
  return api;
}

/** Open a field's calendar and pick a day of the month it shows. */
async function pickDay(field: string, day: string) {
  fireEvent.click(screen.getByLabelText(`${field} date`));
  const cal = screen.getByRole('dialog', { name: `${field}: pick a day` });
  const button = within(cal).getAllByText(day, { selector: 'button' })
    .find(b => !b.classList.contains('is-other')) as HTMLButtonElement;
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}

const frozen = () => camera({
  runtime_enabled: false,
  session: { id: 'sess-1', status: 'frozen', started_at: START, ended_at: UPDATED,
             close_reason: 'operator', points: 10, frames: 20, params: {} },
});

describe('the cards', () => {
  it('shows each camera with its period and when it was last updated', async () => {
    show(overview(camera(), camera({ camera_id: 'cam-2', name: 'Camera 02', slug: 'yard-c3d4' })));
    expect(await screen.findByText('Camera 01', { selector: '.panel-title' })).toBeInTheDocument();
    expect(screen.getByText('Camera 02', { selector: '.panel-title' })).toBeInTheDocument();
    const card = screen.getByText('Camera 01', { selector: '.panel-title' }).closest('.hm-card') as HTMLElement;
    expect(within(card).getByText('Heatmap: ON')).toBeInTheDocument();
    expect(within(card).getByText('Heatmap period')).toBeInTheDocument();
    expect(within(card).getByText('Last updated')).toBeInTheDocument();
  });

  it('shows the configured schedule without letting the page change it', async () => {
    show(overview(camera()));
    expect(await screen.findByText(/Mon, Tue, 08:00 → 18:00/)).toBeInTheDocument();
  });

  it('says so when a camera has no heatmap yet', async () => {
    show(overview(camera({ session: null, latest_artifact: null })),
         { [rangeKeys('cam-1').image]: { status: 404, body: { detail: 'nothing' } } });
    expect(await screen.findByText('No heatmap yet')).toBeInTheDocument();
  });

  it('marks a frozen camera as off rather than looking the same as on', async () => {
    show(overview(frozen()));
    expect(await screen.findByText('Heatmap: OFF (frozen)')).toBeInTheDocument();
    expect(screen.getByText('Frozen at')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Turn On' })).toBeInTheDocument();
  });

  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  it('tells an operator when a camera is simply outside its window', async () => {
    // A day that is definitely NOT today, so the assertion does not depend on
    // when the suite happens to run -- the card now actually evaluates the
    // schedule, so a fixed Mon/Tue window would pass or fail by the calendar.
    const notToday = DAYS[(new Date().getDay() + 3) % 7];
    show(overview(camera({ session: null,
      schedule: { active_days: [notToday], active_hours: null } })));
    expect(await screen.findByText(/Outside its schedule/)).toBeInTheDocument();
  });

  it('does not claim a 24x7 camera is outside its schedule', async () => {
    // THE BUG THIS REPLACES. A camera added minutes ago on an all-day, all-week
    // schedule was told "Outside its schedule -- it will start again in its
    // next window", because the card read "no session" as "out of window"
    // without ever consulting the schedule. The honest answer is that it is in
    // its window and no overlay has arrived yet.
    show(overview(camera({ session: null, latest_artifact: null,
      schedule: { active_days: [...DAYS.slice(1), 'Sun'], active_hours: null } })));
    expect(await screen.findByText('Heatmap: waiting for the first overlay'))
      .toBeInTheDocument();
    expect(screen.queryByText(/Outside its schedule/)).toBeNull();
  });

  it('offers AI Config when no camera has a heatmap at all', async () => {
    const onOpenConfig = vi.fn();
    mockApi({ 'GET /api/analytics/heatmaps': overview() });
    render(<HeatmapOverlayTab onOpenConfig={onOpenConfig} />);
    expect(await screen.findByText('No camera has a heatmap yet')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open AI Config' }));
    expect(onOpenConfig).toHaveBeenCalled();
  });
});

describe('turning a camera off', () => {
  it('warns first and promises the schedule is not changed', async () => {
    const api = show(overview(camera()));
    fireEvent.click(await screen.findByRole('button', { name: 'Turn Off' }));
    expect(await screen.findByText('Turn off Heatmap Overlay?')).toBeInTheDocument();
    expect(screen.getByText(/freezes the current heatmap/)).toBeInTheDocument();
    expect(screen.getByText(/schedule is not changed/)).toBeInTheDocument();
    expect(api.called(RUNTIME)).toBe(false);
  });

  it('cancel changes nothing at all', async () => {
    const api = show(overview(camera()));
    fireEvent.click(await screen.findByRole('button', { name: 'Turn Off' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.queryByText('Turn off Heatmap Overlay?')).not.toBeInTheDocument());
    expect(api.called(RUNTIME)).toBe(false);
  });

  it('confirming sends the switch and keeps the resume option open', async () => {
    const api = show(overview(camera()), {
      [RUNTIME]: { camera: 'gate-a1b2', runtime_enabled: false, mode: 'resume' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Turn Off' }));
    const dialog = await screen.findByText('Turn off Heatmap Overlay?');
    const modal = dialog.closest('.modal') as HTMLElement;
    fireEvent.click(within(modal).getByRole('button', { name: 'Turn Off' }));
    await waitFor(() => expect(api.called(RUNTIME)).toBe(true));
    expect(api.bodyOf(RUNTIME)).toEqual({ enabled: false, mode: 'resume', reason: null });
  });
});

describe('turning a camera back on', () => {
  it('asks whether to resume or start a new session', async () => {
    show(overview(frozen()));
    fireEvent.click(await screen.findByRole('button', { name: 'Turn On' }));
    expect(await screen.findByText('How do you want to continue?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume previous session' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start new session' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('resuming continues the frozen session', async () => {
    const api = show(overview(frozen()), {
      [RUNTIME]: { camera: 'gate-a1b2', runtime_enabled: true, mode: 'resume' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Turn On' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Resume previous session' }));
    await waitFor(() =>
      expect(api.bodyOf(RUNTIME)).toEqual({ enabled: true, mode: 'resume', reason: null }));
  });

  it('starting a new one says so', async () => {
    const api = show(overview(frozen()), {
      [RUNTIME]: { camera: 'gate-a1b2', runtime_enabled: true, mode: 'new' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Turn On' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Start new session' }));
    await waitFor(() =>
      expect(api.bodyOf(RUNTIME)).toEqual({ enabled: true, mode: 'new', reason: null }));
  });

  it('does not ask when there is nothing to resume', async () => {
    const api = show(overview(camera({ runtime_enabled: false, session: null,
                                       latest_artifact: null })), {
      [RUNTIME]: { camera: 'gate-a1b2', runtime_enabled: true, mode: 'new' },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Turn On' }));
    await waitFor(() =>
      expect(api.bodyOf(RUNTIME)).toEqual({ enabled: true, mode: 'new', reason: null }));
    expect(screen.queryByText('How do you want to continue?')).not.toBeInTheDocument();
  });
});

describe('what the page does NOT do', () => {
  it('asks only for the overview and the pictures — it starts no analytics', async () => {
    const api = show(overview(camera()));
    await screen.findByText('Camera 01', { selector: '.panel-title' });
    await waitFor(() => expect(api.called(rangeKeys('cam-1').image)).toBe(true));
    for (const call of api.calls) {
      expect(call.startsWith('GET /api/analytics/heatmaps')).toBe(true);
    }
  });

  it('tells a role without the capability instead of retrying for ever', async () => {
    mockApi({ 'GET /api/analytics/heatmaps': { status: 403, body: { detail: 'denied' } } });
    render(<HeatmapOverlayTab />);
    expect(await screen.findByText('Heatmaps are not available to your role')).toBeInTheDocument();
  });
});

describe('the heatmap of any period', () => {
  it('opens on the first camera alphabetically, from today 00:00 up to now', async () => {
    const api = show(overview(camera({ camera_id: 'cam-z', name: 'Zulu' }),
                              camera({ camera_id: 'cam-a', name: 'Alpha' })));
    const select = await screen.findByLabelText('Camera') as HTMLSelectElement;
    expect(select.value).toBe('cam-a');
    expect((screen.getByLabelText('From time') as HTMLInputElement).value).toBe('00:00');
    expect((screen.getByLabelText('Up to now') as HTMLInputElement).checked).toBe(true);
    await waitFor(() => expect(api.called(rangeKeys('cam-a').image)).toBe(true));
    expect(await screen.findByText(/12 five-minute blocks \+ the current one/)).toBeInTheDocument();
    expect(screen.getByText('the live camera frame')).toBeInTheDocument();
    // Detections are per frame, not people, so the page does not show a count.
    expect(screen.queryByText(/detections/)).not.toBeInTheDocument();
  });

  it("puts today's cards above the any-period viewer", async () => {
    show(overview(camera()));
    const cards = await screen.findByText('Today, 00:00 → now', { selector: 'h4' });
    const viewer = screen.getByText('Any other period');
    expect(cards.compareDocumentPosition(viewer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('clicking a camera card shows that camera', async () => {
    const api = show(overview(camera({ camera_id: 'cam-a', name: 'Alpha' }),
                              camera({ camera_id: 'cam-b', name: 'Bravo' })));
    const card = (await screen.findByText('Bravo', { selector: '.panel-title' })).closest('.hm-card') as HTMLElement;
    expect(within(card).queryByRole('button', { name: /View by date|Shown above/ })).toBeNull();
    fireEvent.click(card.querySelector('.hm-pick') as HTMLElement);
    await waitFor(() => expect(api.called(rangeKeys('cam-b').image)).toBe(true));
    expect((screen.getByLabelText('Camera') as HTMLSelectElement).value).toBe('cam-b');
  });

  it('a fixed period asks for exactly that period', async () => {
    const start = new Date(2026, 8, 18, 9, 0);
    const end = new Date(2026, 8, 18, 18, 30);
    const k = rangeKeys('cam-1', start, end);
    const api = show(overview(camera()), { [k.summary]: rangeReply('cam-1', { includes_live: false }),
                                           [k.image]: IMAGE });
    await screen.findByLabelText('Camera');
    fireEvent.click(screen.getByLabelText('Up to now'));
    await pickDay('From', '18');
    fireEvent.change(screen.getByLabelText('From time'), { target: { value: '09:00' } });
    await pickDay('To', '18');
    fireEvent.change(screen.getByLabelText('To time'), { target: { value: '18:30' } });
    await waitFor(() => expect(api.called(k.image)).toBe(true));
  });

  it('greys out the days with no heatmap, and keeps today pickable', async () => {
    show(overview(camera()));
    await screen.findByText(/Heatmap available from/);
    fireEvent.click(screen.getByLabelText('From date'));
    const cal = screen.getByRole('dialog', { name: 'From: pick a day' });
    const day = (n: string) => within(cal).getAllByText(n, { selector: 'button' })
      .find(b => !b.classList.contains('is-other')) as HTMLButtonElement;
    expect(day('18')).toBeEnabled();
    expect(day('19')).toBeDisabled();                    // nothing stored that day
    expect(day('19')).toHaveAttribute('title', 'No heatmap stored for this day');
    expect(day('20')).toBeEnabled();                     // today
    expect(day('21')).toBeDisabled();                    // the future
  });

  it('says from when a heatmap is available and how much is kept', async () => {
    show(overview(camera()));
    expect(await screen.findByText(/Heatmap available from/)).toBeInTheDocument();
    expect(screen.getByText(/30 days or 500 MB, whichever comes first · using 600 KB/)).toBeInTheDocument();
  });

  it('downloads the picture on screen with a name that says what it is', async () => {
    const clicked: HTMLAnchorElement[] = [];
    const spy = vi.spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) { clicked.push(this); });
    show(overview(camera()));
    await screen.findByText('Any other period');
    const viewer = document.querySelector('section.hm-range') as HTMLElement;
    const button = await within(viewer).findByRole('button', { name: /Download/ });
    fireEvent.click(button);
    expect(clicked).toHaveLength(1);
    expect(clicked[0].href).toBe('blob:heatmap');
    expect(clicked[0].download).toMatch(/^heatmap_Camera-01_20260920-0000_to_20260920-\d{4}\.jpg$/);
    spy.mockRestore();
  });

  it('says so when nothing is stored for the period', async () => {
    const k = rangeKeys('cam-1');
    show(overview(camera()), { [k.summary]: rangeReply('cam-1', { blocks: 0, includes_live: false,
                                                                  covered_from: null, covered_to: null,
                                                                  background_at: null }),
                               [k.image]: { status: 404, body: { detail: 'nothing' } } });
    expect(await screen.findByText(/No heat or camera frame is stored for this period yet/)).toBeInTheDocument();
  });
});

describe('the colour scale', () => {
  it('has no scale switch: colours are always relative', async () => {
    show(overview(camera()));
    await screen.findByText('Camera 01', { selector: '.panel-title' });
    expect(screen.queryByRole('button', { name: 'Fixed' })).toBeNull();
    expect(screen.queryByText('Colour scale')).toBeNull();
  });

  it('cards show today, 00:00 to now', async () => {
    show(overview(camera()));
    const card = (await screen.findByText('Camera 01', { selector: '.panel-title' }))
      .closest('.hm-card') as HTMLElement;
    expect(within(card).getByText('Today, 00:00 → now')).toBeInTheDocument();
  });
});
