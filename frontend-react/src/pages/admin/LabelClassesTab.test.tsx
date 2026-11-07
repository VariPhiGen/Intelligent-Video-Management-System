/**
 * The taxonomy editor.
 *
 * The behaviour worth pinning is RETIRE vs DELETE. A class in use cannot be
 * deleted — the server answers 409 — so the row must not offer it. A UI that
 * offers a button the server always refuses teaches people to distrust every
 * other button on the page.
 *
 * The second theme is the KEY. It is what annotations store and what exports
 * carry: PATCH will not accept it, so the form validates it against the
 * server's own rule rather than letting a stray capital become a 422.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { mockApi } from '@/test/api';

vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));

import { LabelClassesTab, slugify } from './LabelClassesTab';

const CLASSES = [
  { key: 'forklift', label: 'Forklift', color: '#ff8800', sort_order: 0,
    active: true, description: null, annotation_count: 12 },
  { key: 'unused', label: 'Unused', color: '#333333', sort_order: 1,
    active: true, description: null, annotation_count: 0 },
  { key: 'old', label: 'Old', color: '#808080', sort_order: 2,
    active: false, description: null, annotation_count: 3 },
];

function api(over: Record<string, unknown> = {}) {
  return mockApi({ 'GET /api/label-classes': CLASSES, ...over });
}

const rowFor = async (key: string) => {
  await screen.findByDisplayValue(
    CLASSES.find(c => c.key === key)!.label);
  return document.querySelector(`[data-class-key="${key}"]`) as HTMLElement;
};

describe('LabelClassesTab', () => {
  it('shows how many annotations name each class', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('forklift');
    expect(within(row).getByText('12')).toBeInTheDocument();
  });

  it('offers retire — not delete — for a class in use', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('forklift');
    expect(row.querySelector('[data-action="retire"]')).not.toBeNull();
    expect(row.querySelector('[data-action="delete"]')).toBeNull();
  });

  it('offers delete only for a class nothing references', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('unused');
    expect(row.querySelector('[data-action="delete"]')).not.toBeNull();
    expect(row.querySelector('[data-action="retire"]')).toBeNull();
  });

  it('offers restore for a retired class, and never delete', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('old');
    expect(row.querySelector('[data-action="restore"]')).not.toBeNull();
    expect(row.querySelector('[data-action="delete"]')).toBeNull();
  });

  it('shows the key, which is what exports carry', async () => {
    api();
    render(<LabelClassesTab />);
    expect(await screen.findByText('forklift')).toBeInTheDocument();
  });

  // ── Editing in place ──────────────────────────────────────────────────────

  it('PATCHes only the class whose name changed, and only the editable fields',
     async () => {
    const a = api({ 'PATCH /api/label-classes/forklift': { ...CLASSES[0], label: 'Fork truck' } });
    render(<LabelClassesTab />);
    const row = await rowFor('forklift');
    await userEvent.clear(within(row).getByLabelText('forklift name'));
    await userEvent.type(within(row).getByLabelText('forklift name'), 'Fork truck');

    await userEvent.click(await screen.findByText('Save changes'));

    await waitFor(() => expect(a.called('PATCH /api/label-classes/forklift')).toBe(true));
    expect(a.bodyOf('PATCH /api/label-classes/forklift'))
      .toEqual({ label: 'Fork truck', color: '#ff8800', sort_order: 0 });
    // The untouched row is left alone.
    expect(a.called('PATCH /api/label-classes/unused')).toBe(false);
  });

  it('will not let a pending edit be lost to a retire', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('forklift');
    await userEvent.type(within(row).getByLabelText('forklift name'), 'x');
    expect(row.querySelector<HTMLButtonElement>('[data-action="retire"]')!.disabled)
      .toBe(true);
  });

  it('refuses to save a class whose name has been emptied', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('forklift');
    await userEvent.clear(within(row).getByLabelText('forklift name'));
    expect(await screen.findByText(/every class needs a name/i)).toBeInTheDocument();
    expect(screen.getByText('Save changes')).toBeDisabled();
  });

  it('discards edits back to what the server said', async () => {
    api();
    render(<LabelClassesTab />);
    const row = await rowFor('forklift');
    const name = within(row).getByLabelText('forklift name');
    await userEvent.type(name, 'zzz');
    await userEvent.click(await screen.findByText('Discard'));
    expect(name).toHaveValue('Forklift');
  });

  // ── The key ───────────────────────────────────────────────────────────────

  it('derives a valid key from the name, so nobody learns the rule', async () => {
    expect(slugify('Forklift truck')).toBe('forklift_truck');
    expect(slugify('  Hi-Vis Vest!  ')).toBe('hi_vis_vest');

    api();
    render(<LabelClassesTab />);
    await userEvent.click(await screen.findByText('+ Add class'));
    await userEvent.type(screen.getByLabelText('Name'), 'Hi-Vis Vest');
    expect(screen.getByLabelText('Key')).toHaveValue('hi_vis_vest');
  });

  it('rejects a bad key in the form instead of sending a 422', async () => {
    const a = api();
    render(<LabelClassesTab />);
    await userEvent.click(await screen.findByText('+ Add class'));
    await userEvent.type(screen.getByLabelText('Name'), 'Truck');
    await userEvent.clear(screen.getByLabelText('Key'));
    await userEvent.type(screen.getByLabelText('Key'), 'Fork Lift');

    expect(screen.getByText(/lowercase letters, digits and underscores/i))
      .toBeInTheDocument();
    expect(screen.getByText('Add class')).toBeDisabled();
    expect(a.called('POST /api/label-classes')).toBe(false);
  });

  it('will not offer to create a key that already exists', async () => {
    api();
    render(<LabelClassesTab />);
    await userEvent.click(await screen.findByText('+ Add class'));
    await userEvent.type(screen.getByLabelText('Name'), 'Anything');
    await userEvent.clear(screen.getByLabelText('Key'));
    await userEvent.type(screen.getByLabelText('Key'), 'forklift');

    expect(screen.getByText(/already exists/i)).toBeInTheDocument();
    expect(screen.getByText('Add class')).toBeDisabled();
  });

  it('sends the whole new class, with an omitted description as null', async () => {
    const a = api({ 'POST /api/label-classes': { status: 201, body: CLASSES[0] } });
    render(<LabelClassesTab />);
    await userEvent.click(await screen.findByText('+ Add class'));
    await userEvent.type(screen.getByLabelText('Name'), 'Pallet');
    await userEvent.click(screen.getByText('Add class'));

    await waitFor(() => expect(a.called('POST /api/label-classes')).toBe(true));
    expect(a.bodyOf('POST /api/label-classes')).toEqual({
      key: 'pallet', label: 'Pallet', color: '#ff8800',
      sort_order: 0, description: null,
    });
  });

  // ── Empty state ───────────────────────────────────────────────────────────

  it('says the taxonomy is empty rather than showing a bare header row',
     async () => {
    api({ 'GET /api/label-classes': [] });
    render(<LabelClassesTab />);
    expect(await screen.findByText(/no label classes yet/i)).toBeInTheDocument();
  });
});
