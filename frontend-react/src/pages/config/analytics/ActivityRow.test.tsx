/**
 * ActivityRow — one activity's per-camera settings, rendered from the
 * activity's definition. People Gathering's behaviour settings are the case.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ActivityMeta } from './activities';
import { ActivityRow } from './ActivityRow';
import type { EditActivity } from './analyticsConfig';

const GATHERING: ActivityMeta = {
  key: 'people_gathering', label: 'People gathering', color: '#ff8a65', status: 'available', zone_rule: 'optional',
  params_schema: [
    { key: 'required_duration_s', label: 'Gathering duration', kind: 'number', default: 60, unit: 's', min: 1, max: 86400 },
    { key: 'last_time', label: 'Reset after no gathering', kind: 'number', default: 30, unit: 's', min: 1, max: 3600 },
    { key: 'min_group_size', label: 'Minimum people', kind: 'number', default: 2, min: 2, max: 50, integer: true },
    { key: 'proximity_factor', label: 'Proximity factor', kind: 'number', default: 1.35, unit: '× box width', min: 0.5, max: 10 },
    { key: 'cooldown_s', label: 'Cooldown', kind: 'number', default: 100, unit: 's', min: 1, max: 86400 },
  ],
};

function renderRow(params: Record<string, unknown>) {
  const onPatchParams = vi.fn();
  const act: EditActivity = {
    type: 'people_gathering', regions: [],
    params: { active_hours: null, active_days: ['Mon'], ...params } as EditActivity['params'],
  };
  render(<ActivityRow act={act} catalog={[GATHERING]} byKey={{ people_gathering: GATHERING }}
                      regions={{}} regionOrder={[]} paramsOpen
                      onToggleParams={vi.fn()} onSetType={vi.fn()} onAddZone={vi.fn()} onRemoveZone={vi.fn()}
                      onPatchParams={onPatchParams} onToggleDay={vi.fn()} onRemove={vi.fn()}
                      onInvalidChange={vi.fn()} />);
  return onPatchParams;
}

const input = (label: string) =>
  screen.getByText(label).parentElement!.querySelector('input') as HTMLInputElement;

describe('ActivityRow — People Gathering settings', () => {
  it('offers every setting the definition declares, with the camera’s values', () => {
    renderRow({ required_duration_s: 120, last_time: 20, min_group_size: 5, proximity_factor: 2, cooldown_s: 300 });
    expect(input('Gathering duration').value).toBe('120');
    expect(input('Reset after no gathering').value).toBe('20');
    expect(input('Minimum people').value).toBe('5');
    expect(input('Proximity factor').value).toBe('2');
    expect(input('Cooldown').value).toBe('300');
    expect(input('Minimum people')).toHaveAttribute('step', '1');
    expect(input('Minimum people')).toHaveAttribute('min', '2');
    expect(input('Proximity factor')).toHaveAttribute('min', '0.5');
    expect(input('Proximity factor')).toHaveAttribute('max', '10');
    expect(screen.getByText('× box width')).toBeInTheDocument();
  });

  it('shows the default the engine runs with for a setting the camera never stored', () => {
    const patch = renderRow({ required_duration_s: 60, last_time: 30 });
    expect(input('Minimum people').value).toBe('2');
    expect(input('Proximity factor').value).toBe('1.35');
    expect(input('Cooldown').value).toBe('100');
    expect(patch).not.toHaveBeenCalled();
  });

  it('keeps a whole-number setting whole and within its bounds', () => {
    const patch = renderRow({});
    fireEvent.change(input('Minimum people'), { target: { value: '3.6' } });
    fireEvent.blur(input('Minimum people'));
    expect(patch).toHaveBeenLastCalledWith({ min_group_size: 4 });
    fireEvent.change(input('Minimum people'), { target: { value: '1' } });
    fireEvent.blur(input('Minimum people'));
    expect(patch).toHaveBeenLastCalledWith({ min_group_size: 2 });
    fireEvent.change(input('Proximity factor'), { target: { value: '2.25' } });
    fireEvent.blur(input('Proximity factor'));
    expect(patch).toHaveBeenLastCalledWith({ proximity_factor: 2.25 });
  });
});
