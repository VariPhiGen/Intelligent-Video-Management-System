/**
 * The annotate overlay: the class picker, and the ONE toolbar rule.
 *
 * Saving without a class must be impossible in the UI rather than merely
 * rejected by the server: the frame is captured at POINTER-DOWN, so a save
 * that bounces off a 422 has lost the exact pixels the box was drawn on, and
 * the footage behind them may since have aged out.
 *
 * The layout invariant is the other half. `.pb-annotate-bar` centres itself on
 * one fixed bottom offset, so two of them are two panels on identical pixels —
 * which is exactly what shipped: the class picker and the save actions
 * overlapped as soon as a box was drawn. Anything that renders a second bar is
 * a regression, so the count is asserted in both states.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));

import { AnnotateLayer, readableOn } from './AnnotateLayer';
import type { LabelClass } from '@/lib/labelClasses';

const CLASSES: LabelClass[] = [
  { key: 'forklift', label: 'Forklift', color: '#ff8800', sort_order: 0,
    active: true, description: null, annotation_count: 0 },
  { key: 'pallet', label: 'Pallet', color: '#00aa88', sort_order: 1,
    active: true, description: null, annotation_count: 0 },
];

/** A <video> that reports a frame, which is what onPointerDown gates on. */
function videoEl(w = 640, h = 360) {
  const v = document.createElement('video');
  Object.defineProperty(v, 'videoWidth', { value: w });
  Object.defineProperty(v, 'videoHeight', { value: h });
  return v;
}

/** jsdom has no canvas: getContext throws and toBlob does not exist, so the
 *  drawing path has to be stubbed before a drag can be driven at all. */
function stubCanvas(w = 640, h = 360) {
  const ctx = {
    setTransform() {}, clearRect() {}, save() {}, restore() {},
    fillRect() {}, strokeRect() {}, drawImage() {}, fillText() {},
    measureText: () => ({ width: 40 }),
    globalAlpha: 1, fillStyle: '', strokeStyle: '', lineWidth: 1,
    font: '', textBaseline: '',
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext')
    .mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob')
    .mockImplementation(((cb: BlobCallback) =>
      cb(new Blob(['jpeg'], { type: 'image/jpeg' }))) as never);
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect')
    .mockReturnValue({ left: 0, top: 0, right: w, bottom: h, width: w, height: h,
                       x: 0, y: 0, toJSON: () => ({}) });
  return ctx;
}

function setup(classes: LabelClass[] = CLASSES,
               extra: { onSimilar?: () => void } = {}) {
  const videoRef = { current: videoEl() };
  return render(
    <AnnotateLayer videoRef={videoRef} camera="gate-cam" classes={classes}
                   epochOf={() => 1790000000} {...extra} />,
  );
}

const bars = () => document.querySelectorAll('.pb-annotate-bar');

/** Draw a box, the way a pointer does. */
async function drawBox() {
  const canvas = document.querySelector('.pb-annotate-canvas') as HTMLCanvasElement;
  // jsdom implements neither, and the handler calls them on the event target.
  (canvas as unknown as Record<string, unknown>).setPointerCapture = () => {};
  (canvas as unknown as Record<string, unknown>).releasePointerCapture = () => {};
  await act(async () => {
    fireEvent.pointerDown(canvas, { clientX: 80, clientY: 60, pointerId: 1 });
  });
  await act(async () => {
    fireEvent.pointerMove(canvas, { clientX: 260, clientY: 240, pointerId: 1 });
    fireEvent.pointerUp(canvas, { clientX: 260, clientY: 240, pointerId: 1 });
  });
}

describe('AnnotateLayer class picker', () => {
  it('offers every class it was given', () => {
    setup();
    const select = screen.getByLabelText(/class/i) as HTMLSelectElement;
    expect([...select.options].map(o => o.value)).toEqual(['forklift', 'pallet']);
  });

  it('preselects one, so a save always carries a class', () => {
    setup();
    expect((screen.getByLabelText(/class/i) as HTMLSelectElement).value)
      .toBe('forklift');
  });

  it('says so when the taxonomy is empty instead of offering a dead picker', () => {
    setup([]);
    expect(screen.getByText(/no label classes/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/class/i)).toBeNull();
  });

  it('still explains how to draw a box', () => {
    setup();
    expect(screen.getByText(/drag a box/i)).toBeInTheDocument();
  });

  it('adopts a class once the parent has fetched the taxonomy', () => {
    // SinglePlayer fetches the taxonomy after mount, so the layer can legally
    // mount with none — and the initial '' would be saved as a missing class.
    const { rerender } = render(
      <AnnotateLayer videoRef={{ current: videoEl() }} camera="gate-cam"
                     classes={[]} epochOf={() => 1790000000} />);
    expect(screen.queryByLabelText(/class/i)).toBeNull();

    rerender(
      <AnnotateLayer videoRef={{ current: videoEl() }} camera="gate-cam"
                     classes={CLASSES} epochOf={() => 1790000000} />);
    expect((screen.getByLabelText(/class/i) as HTMLSelectElement).value)
      .toBe('forklift');
  });

  it('re-seeds when the selected class is retired from under the operator', () => {
    const { rerender } = setup();
    expect((screen.getByLabelText(/class/i) as HTMLSelectElement).value)
      .toBe('forklift');

    rerender(
      <AnnotateLayer videoRef={{ current: videoEl() }} camera="gate-cam"
                     classes={[CLASSES[1]]} epochOf={() => 1790000000} />);
    expect((screen.getByLabelText(/class/i) as HTMLSelectElement).value)
      .toBe('pallet');
  });
});

describe('AnnotateLayer — one toolbar', () => {
  it('renders a single bar before a box is drawn', () => {
    setup();
    expect(bars()).toHaveLength(1);
  });

  it('renders a single bar with the taxonomy empty', () => {
    setup([]);
    expect(bars()).toHaveLength(1);
  });

  it('keeps the actions INSIDE that bar rather than stacking a second one',
     async () => {
    stubCanvas();
    setup(CLASSES, { onSimilar: () => {} });
    await drawBox();

    // The regression: two absolutely-positioned bars on identical coordinates.
    expect(bars()).toHaveLength(1);
    const bar = bars()[0];
    expect(bar.querySelector('select')).not.toBeNull();
    expect(bar.querySelector('[aria-label="Discard this box"]')).not.toBeNull();
    expect(screen.getByText('Save')).toBeInTheDocument();
    expect(screen.getByText(/find similar/i)).toBeInTheDocument();
  });

  it('swaps the instruction out for the actions, never showing both', async () => {
    stubCanvas();
    setup();
    expect(screen.getByText(/drag a box/i)).toBeInTheDocument();
    await drawBox();
    expect(screen.queryByText(/drag a box/i)).toBeNull();
    expect(screen.getByText('Save')).toBeInTheDocument();
  });

  it('does not offer Find similar where Smart Search is not wired up',
     async () => {
    stubCanvas();
    setup();                                   // no onSimilar
    await drawBox();
    expect(screen.queryByText(/find similar/i)).toBeNull();
  });

  it('discards the box on Escape, returning the bar to its instruction',
     async () => {
    stubCanvas();
    setup();
    await drawBox();
    expect(screen.getByText('Save')).toBeInTheDocument();

    await act(async () => {
      fireEvent.keyDown(window, { key: 'Escape' });
    });
    expect(screen.queryByText('Save')).toBeNull();
    expect(screen.getByText(/drag a box/i)).toBeInTheDocument();
    expect(bars()).toHaveLength(1);
  });
});

describe('readableOn', () => {
  it('puts dark text on a light class colour and light text on a dark one', () => {
    expect(readableOn('#ffff00')).toBe('#0B0E12');   // yellow
    expect(readableOn('#ffffff')).toBe('#0B0E12');
    expect(readableOn('#1b1b1b')).toBe('#fff');
    expect(readableOn('#ff8800')).toBe('#fff');      // the default orange
  });

  it('falls back to white rather than throwing on a colour it cannot parse', () => {
    expect(readableOn('rebeccapurple')).toBe('#fff');
  });
});
