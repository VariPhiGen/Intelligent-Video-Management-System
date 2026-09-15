/**
 * ActivityTypesTab — the activities come from the analytics engine; an
 * administrator can rename and recolour them, and nothing else.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { mockApi } from '@/test/api';

vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));

import { ActivityTypesTab, describeField } from './ActivityTypesTab';

const TYPES = [
  {
    key: 'stray_parking', label: 'Stray parking', color: '#ffb020', status: 'available', zone_rule: 'required',
    description: 'A vehicle staying inside a no-parking zone.', definition_version: 1,
    params_schema: [
      { key: 'vehicle_classes', label: 'Vehicle types', kind: 'list', default: ['car', 'truck'],
        options: ['car', 'motorcycle', 'bus', 'truck'], min_items: 1, configurable: true },
      { key: 'required_duration_s', label: 'Required parking duration', kind: 'number', default: 600,
        unit: 's', min: 1, max: 86400, configurable: true },
    ],
  },
  { key: 'idle_worker', label: 'Idle worker', color: '#4fd1c5', status: 'hold', zone_rule: 'required',
    description: 'On hold', params_schema: [] },
];

function api() {
  return mockApi({
    'GET /api/cameras/analytics/types': TYPES,
    'GET /api/cameras?limit=1000': [],
    'PUT /api/cameras/analytics/types': TYPES.map(t => (t.key === 'stray_parking' ? { ...t, label: 'Parking' } : t)),
  });
}

describe('ActivityTypesTab', () => {
  it('lists the engine’s activities with their status and offers no way to invent one', async () => {
    api();
    render(<ActivityTypesTab />);
    const row = (await screen.findByText('stray_parking')).closest('[data-type-key]') as HTMLElement;
    expect(within(row).getByText('Available')).toBeInTheDocument();
    const hold = screen.getByText('idle_worker').closest('[data-type-key]') as HTMLElement;
    expect(within(hold).getByText('On hold')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /add type/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /add setting/i })).toBeNull();
    expect(screen.queryByTitle(/remove type/i)).toBeNull();
  });

  it('shows the settings the engine defines, read-only, in words', async () => {
    api();
    render(<ActivityTypesTab />);
    const row = (await screen.findByText('stray_parking')).closest('[data-type-key]') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: /2 settings/i }));
    expect(within(row).getByText('Required parking duration')).toBeInTheDocument();
    expect(within(row).getByText('default 600 s · range 1–86400')).toBeInTheDocument();
    expect(within(row).getByText('Needs at least one zone · definition v1')).toBeInTheDocument();
    expect(within(row).queryByText('required_duration_s')).toBeNull();
  });

  it('saves only names and colours', async () => {
    const calls = api();
    render(<ActivityTypesTab />);
    fireEvent.change(await screen.findByLabelText('stray_parking name'), { target: { value: 'Parking' } });
    fireEvent.click(screen.getByRole('button', { name: /save names/i }));
    await waitFor(() => expect(calls.called('PUT /api/cameras/analytics/types')).toBe(true));
    expect(calls.bodyOf('PUT /api/cameras/analytics/types')).toEqual({ types: [
      { key: 'stray_parking', label: 'Parking', color: '#ffb020' },
      { key: 'idle_worker', label: 'Idle worker', color: '#4fd1c5' },
    ] });
  });

  it('describes a number setting by its default, unit and range', () => {
    expect(describeField({ key: 'proximity_factor', label: 'Proximity factor', kind: 'number', default: 1.35,
                           unit: '× box width', min: 0.5, max: 10 })).toBe('default 1.35 × box width · range 0.5–10');
    expect(describeField({ key: 'min_group_size', label: 'Minimum people', kind: 'number', default: 2,
                           min: 2, max: 50, integer: true })).toBe('default 2 · range 2–50');
  });

  it('describes a list setting by its choices', () => {
    expect(describeField(TYPES[0].params_schema[0] as any)).toBe('default car, truck · choices car, motorcycle, bus, truck');
  });
});
