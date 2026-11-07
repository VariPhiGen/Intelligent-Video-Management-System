import { expect, it } from 'vitest';
import { modelRows } from './deepstream';

it('reads a bare array of names', () => {
  expect(modelRows(['a', 'b'])).toEqual({ rows: [{ name: 'a' }, { name: 'b' }], active: null });
});

it('reads an array under `models` and flags the active one', () => {
  expect(modelRows({ active: 'b', models: [{ name: 'a' }, { name: 'b', batch_size: 8 }] })).toEqual({
    active: 'b',
    rows: [{ name: 'a', active: false }, { name: 'b', batch_size: 8, active: true }],
  });
});

it('reads a map of name → details', () => {
  expect(modelRows({ current: 'plant_v2', models: { plant_v2: { batch_size: 8 }, school_v2: { batch_size: 1 } } }))
    .toEqual({
      active: 'plant_v2',
      rows: [{ name: 'plant_v2', batch_size: 8, active: true }, { name: 'school_v2', batch_size: 1, active: false }],
    });
});

it('reads a top-level name → details map', () => {
  expect(modelRows({ plant_v2: { onnx: 'p.onnx' } })?.rows).toEqual([{ name: 'plant_v2', onnx: 'p.onnx' }]);
});

it('keeps a row-level active flag the pipeline already sent', () => {
  expect(modelRows({ active: 'a', models: [{ name: 'a', active: false }] })?.rows).toEqual([{ name: 'a', active: false }]);
});

it('gives up on a shape with no list in it', () => {
  expect(modelRows({ status: 'ok' })).toBeNull();
  expect(modelRows('nope')).toBeNull();
});
