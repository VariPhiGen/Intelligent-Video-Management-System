/**
 * The screen that decides whether a drawn box survives.
 *
 * Two behaviours here are not cosmetic. Relabel exists because delete is
 * irreversible — the footage behind the frame may have aged out, so a
 * misclicked class cannot be fixed by redrawing. Keep exists because the
 * default is deletion: a loose annotation carries an expiry and is swept.
 *
 * Three more are pinned because each one shipped broken. The frames are
 * Bearer-gated, so they have to be FETCHED, not pointed at. The box has to be
 * drawn, or this is a wall of scenes with no indication of what was labelled.
 * And a kept reason has to be non-empty, because the API reads
 * `kept_reason or row.kept_reason` — a blank one clears the expiry and leaves
 * the justification null, which is the unanswerable number 048 exists to stop.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { mockApi } from '@/test/api';

vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));

import { AnnotationsTab, boxStyle, keepState } from './AnnotationsTab';
import type { Annotation } from '@/lib/annotations';

// jsdom implements neither, and AuthImg is how every frame on this screen is
// loaded, so without these the grid cannot render at all.
let objectUrlCount = 0;
beforeAll(() => {
  URL.createObjectURL = (() => `blob:mock/${objectUrlCount++}`) as typeof URL.createObjectURL;
  URL.revokeObjectURL = (() => {}) as typeof URL.revokeObjectURL;
});
beforeEach(() => { objectUrlCount = 0; });

const SOON = new Date(Date.now() + 36 * 3600_000).toISOString();

const LOOSE = {
  id: 'abc123', camera_slug: 'gate-cam', class_key: 'forklift',
  captured_at: '2026-09-25T10:00:00Z',
  bbox: { x: 10, y: 20, width: 30, height: 40 },
  frame: { width: 1920, height: 1080 },
  image_path: '/a.jpg', dataset_id: null,
  expires_at: SOON, kept_reason: null, created_by: 'dev',
};
const KEPT = {
  ...LOOSE, id: 'kept1', expires_at: null, kept_reason: 'forklift set',
};

const CLASSES = [
  { key: 'forklift', label: 'Forklift', color: '#ff8800', sort_order: 0,
    active: true, description: null, annotation_count: 12 },
  { key: 'pallet', label: 'Pallet', color: '#00aa88', sort_order: 1,
    active: true, description: null, annotation_count: 3 },
];

function api(over: Record<string, unknown> = {}) {
  return mockApi({
    'GET /api/annotations/stats': { by_class: { forklift: 12, pallet: 3 },
                                    by_camera: { 'gate-cam': 14, 'dock-cam': 1 },
                                    total: 15 },
    'GET /api/annotations?limit=60&offset=0': { annotations: [LOOSE], total: 1 },
    'GET /api/label-classes': CLASSES,
    'GET /api/annotations/abc123/image': { body: 'jpeg-bytes' },
    'GET /api/annotations/kept1/image': { body: 'jpeg-bytes' },
    'PATCH /api/annotations/abc123': { ...LOOSE, class_key: 'pallet' },
    ...over,
  });
}

const tile = async (id: string) =>
  (await screen.findByText('gate-cam')).closest(`[data-ann-id="${id}"]`) as HTMLElement;

describe('AnnotationsTab', () => {
  it('shows a count per class, so under-sampling is visible', async () => {
    api();
    render(<AnnotationsTab />);
    const strip = await screen.findByTestId('ann-counts');
    expect(within(strip).getByText('12')).toBeInTheDocument();
    expect(within(strip).getByText('3')).toBeInTheDocument();
    expect(within(strip).getByText(/15 total/)).toBeInTheDocument();
  });

  it('counts a class with nothing in it — the one worth acting on', async () => {
    // /stats omits classes with no rows, so the empty class was invisible on
    // the one strip whose job is to say what is missing.
    api({ 'GET /api/annotations/stats': { by_class: { forklift: 12 },
                                         by_camera: { 'gate-cam': 12 },
                                         total: 12 } });
    render(<AnnotationsTab />);
    const strip = await screen.findByTestId('ann-counts');
    const pallet = within(strip).getByText('pallet').closest('button')!;
    expect(within(pallet).getByText('0')).toBeInTheDocument();
  });

  it('filters to a class when its count is clicked', async () => {
    const a = api({
      'GET /api/annotations?limit=60&offset=0&class_key=pallet':
        { annotations: [], total: 0 },
    });
    render(<AnnotationsTab />);
    const strip = await screen.findByTestId('ann-counts');
    await userEvent.click(within(strip).getByText('pallet').closest('button')!);
    await waitFor(() => expect(
      a.called('GET /api/annotations?limit=60&offset=0&class_key=pallet')).toBe(true));
  });

  // ── The frames are behind the token ───────────────────────────────────────

  it('FETCHES the frame rather than pointing an <img> at a gated URL', async () => {
    // A plain <img src> cannot carry the Authorization header, so the old
    // markup produced a 401 for every tile on the screen.
    const a = api();
    render(<AnnotationsTab />);
    await waitFor(() => expect(
      a.called('GET /api/annotations/abc123/image')).toBe(true));
    const img = await screen.findByRole('img', { name: /forklift on gate-cam/i });
    expect(img.getAttribute('src')).toMatch(/^blob:/);
  });

  it('draws the box on the frame, in the class colour', async () => {
    api();
    render(<AnnotationsTab />);
    const box = await screen.findByTestId('ann-box');
    expect(box).toBeInTheDocument();
    expect(box.style.borderColor).toBe('rgb(255, 136, 0)');   // #ff8800
  });

  // ── Relabel ───────────────────────────────────────────────────────────────

  it('lets an operator relabel a misclick in place', async () => {
    const a = api();
    render(<AnnotationsTab />);
    const select = await screen.findByLabelText('Class') as HTMLSelectElement;
    expect(select.value).toBe('forklift');
    fireEvent.change(select, { target: { value: 'pallet' } });
    await waitFor(() => expect(a.called('PATCH /api/annotations/abc123')).toBe(true));
    expect(a.bodyOf('PATCH /api/annotations/abc123')).toEqual({ class_key: 'pallet' });
  });

  it('warns that a loose annotation is on a clock', async () => {
    api();
    render(<AnnotationsTab />);
    expect(await screen.findByText(/expires in/i)).toBeInTheDocument();
  });

  // ── Keep, and the reason it demands ───────────────────────────────────────

  it('asks why in a dialog, and sends the reason', async () => {
    const a = api();
    render(<AnnotationsTab />);
    const t = await tile('abc123');
    await userEvent.click(t.querySelector('[data-action="keep"]') as HTMLButtonElement);

    await userEvent.type(screen.getByLabelText(/why is it kept/i),
                         'forklift training set');
    await userEvent.click(screen.getByText('Keep it'));

    await waitFor(() => expect(a.called('PATCH /api/annotations/abc123')).toBe(true));
    expect(a.bodyOf('PATCH /api/annotations/abc123'))
      .toEqual({ keep: true, kept_reason: 'forklift training set' });
  });

  it('will not keep anything on a blank reason', async () => {
    const a = api();
    render(<AnnotationsTab />);
    const t = await tile('abc123');
    await userEvent.click(t.querySelector('[data-action="keep"]') as HTMLButtonElement);

    expect(screen.getByText('Keep it')).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/why is it kept/i), '   ');
    expect(screen.getByText('Keep it')).toBeDisabled();
    expect(a.called('PATCH /api/annotations/abc123')).toBe(false);
  });

  it('does not keep anything if the dialog is cancelled', async () => {
    const a = api();
    render(<AnnotationsTab />);
    const t = await tile('abc123');
    await userEvent.click(t.querySelector('[data-action="keep"]') as HTMLButtonElement);
    await userEvent.click(screen.getByText('Cancel'));
    expect(a.called('PATCH /api/annotations/abc123')).toBe(false);
  });

  it('shows a kept one as kept, with its reason, and offers Release', async () => {
    api({ 'GET /api/annotations?limit=60&offset=0': { annotations: [KEPT], total: 1 } });
    render(<AnnotationsTab />);
    expect(await screen.findByText(/Kept · forklift set/)).toBeInTheDocument();
    const t = await tile('kept1');
    expect(t.querySelector('[data-action="release"]')).not.toBeNull();
    expect(t.querySelector('[data-action="keep"]')).toBeNull();
  });

  // ── Delete ────────────────────────────────────────────────────────────────

  it('warns that deleting cannot be undone, and waits for confirmation',
     async () => {
    const a = api();
    render(<AnnotationsTab />);
    const t = await tile('abc123');
    await userEvent.click(t.querySelector('[data-action="delete"]') as HTMLButtonElement);

    expect(screen.getByText(/cannot be redrawn/i)).toBeInTheDocument();
    expect(a.called('DELETE /api/annotations/abc123')).toBe(false);

    await userEvent.click(screen.getByText('Cancel'));
    expect(a.called('DELETE /api/annotations/abc123')).toBe(false);
  });

  it('deletes once confirmed', async () => {
    const a = api({ 'DELETE /api/annotations/abc123': { status: 204 } });
    render(<AnnotationsTab />);
    const t = await tile('abc123');
    await userEvent.click(t.querySelector('[data-action="delete"]') as HTMLButtonElement);
    await userEvent.click(screen.getByText('Delete it'));
    await waitFor(() => expect(a.called('DELETE /api/annotations/abc123')).toBe(true));
  });

  // ── Shape of the page ─────────────────────────────────────────────────────

  it('says so when the pile is empty rather than showing a blank grid', async () => {
    api({
      'GET /api/annotations?limit=60&offset=0': { annotations: [], total: 0 },
      'GET /api/annotations/stats': { by_class: {}, by_camera: {}, total: 0 },
    });
    render(<AnnotationsTab />);
    expect(await screen.findByText(/no annotations yet/i)).toBeInTheDocument();
  });

  it('distinguishes an empty pile from an over-narrow filter', async () => {
    const a = api({
      'GET /api/annotations?limit=60&offset=0&class_key=pallet':
        { annotations: [], total: 0 },
    });
    render(<AnnotationsTab />);
    const strip = await screen.findByTestId('ann-counts');
    await userEvent.click(within(strip).getByText('pallet').closest('button')!);
    await waitFor(() => expect(
      a.called('GET /api/annotations?limit=60&offset=0&class_key=pallet')).toBe(true));
    expect(await screen.findByText(/nothing matches those filters/i))
      .toBeInTheDocument();
  });

  it('pages when there is more than one screenful', async () => {
    api({
      'GET /api/annotations?limit=60&offset=0': { annotations: [LOOSE], total: 140 },
    });
    render(<AnnotationsTab />);
    expect(await screen.findByText(/Older/)).toBeInTheDocument();
    expect(screen.getByText(/of 140/)).toBeInTheDocument();
  });

  // ── The camera filter ─────────────────────────────────────────────────────

  it('offers the cameras that HAVE annotations, with their counts', async () => {
    // Not the camera registry: 047 keeps camera_slug out of a foreign key, so
    // the registry would offer cameras with nothing and hide a decommissioned
    // one whose samples are still here.
    api();
    render(<AnnotationsTab />);
    const select = await screen.findByLabelText(/filter by camera/i) as HTMLSelectElement;
    expect([...select.options].map(o => o.value))
      .toEqual(['', 'dock-cam', 'gate-cam']);
    expect([...select.options].map(o => o.textContent))
      .toEqual(['All cameras', 'dock-cam (1)', 'gate-cam (14)']);
  });

  it('filters by the camera chosen, in one request', async () => {
    const a = api({
      'GET /api/annotations?limit=60&offset=0&camera=dock-cam':
        { annotations: [], total: 0 },
    });
    render(<AnnotationsTab />);
    const select = await screen.findByLabelText(/filter by camera/i);
    await userEvent.selectOptions(select, 'dock-cam');
    await waitFor(() => expect(
      a.called('GET /api/annotations?limit=60&offset=0&camera=dock-cam')).toBe(true));
    // One option, one request — the old text box needed debouncing to get here.
    expect(a.callCount('GET /api/annotations?limit=60&offset=0&camera=dock-cam'))
      .toBe(1);
  });

  it('does not refetch the taxonomy when the camera filter changes', async () => {
    const a = api({
      'GET /api/annotations?limit=60&offset=0&camera=dock-cam':
        { annotations: [], total: 0 },
    });
    render(<AnnotationsTab />);
    await screen.findByText('gate-cam');
    const before = a.callCount('GET /api/label-classes');

    await userEvent.selectOptions(
      screen.getByLabelText(/filter by camera/i), 'dock-cam');
    await waitFor(() => expect(
      a.called('GET /api/annotations?limit=60&offset=0&camera=dock-cam')).toBe(true));
    expect(a.callCount('GET /api/label-classes')).toBe(before);
  });

  it('drops a selected camera that no longer has any annotations', async () => {
    // Its last annotation was deleted or swept. A select holding a value with
    // no option renders blank and filters everything away.
    const a = api({
      // A tile has to survive the filter, or there is nothing to act on and no
      // mutation to refresh the counts with.
      'GET /api/annotations?limit=60&offset=0&camera=dock-cam':
        { annotations: [LOOSE], total: 1 },
    });
    render(<AnnotationsTab />);
    await userEvent.selectOptions(
      await screen.findByLabelText(/filter by camera/i), 'dock-cam');
    await waitFor(() => expect(
      a.called('GET /api/annotations?limit=60&offset=0&camera=dock-cam')).toBe(true));

    a.set('GET /api/annotations/stats',
          { by_class: { forklift: 12 }, by_camera: { 'gate-cam': 12 }, total: 12 });
    // A mutation is what refreshes the counts; a relabel is the cheapest one.
    fireEvent.change(await screen.findByLabelText('Class'),
                     { target: { value: 'pallet' } });

    await waitFor(() => expect(
      (screen.getByLabelText(/filter by camera/i) as HTMLSelectElement).value)
      .toBe(''));
  });
});

describe('boxStyle', () => {
  const at = (bbox: Annotation['bbox'], frame: Annotation['frame']) =>
    boxStyle({ ...LOOSE, bbox, frame } as Annotation, 16 / 9);

  it('maps straight through when the frame matches the tile', () => {
    // 1920x1080 is 16:9, so there are no bars and percentages are direct.
    expect(at({ x: 960, y: 540, width: 192, height: 108 },
              { width: 1920, height: 1080 }))
      .toEqual({ left: '50%', top: '50%', width: '10%', height: '10%' });
  });

  it('allows for bars top and bottom on a wider-than-tile frame', () => {
    // 32:9 in a 16:9 tile paints half the height, centred.
    const s = at({ x: 0, y: 0, width: 320, height: 90 },
                 { width: 3200, height: 900 });
    expect(s.top).toBe('25%');
    expect(s.height).toBe('5%');
    expect(s.left).toBe('0%');
    expect(s.width).toBe('10%');
  });

  it('allows for bars left and right on a taller-than-tile frame', () => {
    // 1:1 in a 16:9 tile paints 9/16 of the width, centred.
    const s = at({ x: 0, y: 0, width: 100, height: 100 },
                 { width: 1000, height: 1000 });
    expect(s.left).toBe('21.875%');
    expect(s.width).toBe('5.625%');
    expect(s.top).toBe('0%');
    expect(s.height).toBe('10%');
  });

  it('does not divide by zero on a frame the NVR reported as empty', () => {
    const s = at({ x: 0, y: 0, width: 0, height: 0 }, { width: 0, height: 0 });
    expect(Object.values(s).every(v => !String(v).includes('NaN'))).toBe(true);
  });
});

describe('keepState', () => {
  it('reads a dataset-owned one as kept, naming the dataset', () => {
    expect(keepState({ ...LOOSE, dataset_id: 7, expires_at: null } as Annotation).text)
      .toBe('Kept · dataset #7');
  });

  it('separates a decision from a countdown', () => {
    expect(keepState(KEPT as Annotation).cls).toBe('ann-keep-kept');
    expect(keepState(LOOSE as Annotation).cls).toBe('ann-keep-soon');
  });

  it('says a passed deadline is expiring now rather than a negative countdown', () => {
    const past = new Date(Date.now() - 3600_000).toISOString();
    expect(keepState({ ...LOOSE, expires_at: past } as Annotation).text)
      .toBe('Expiring now');
  });
});
