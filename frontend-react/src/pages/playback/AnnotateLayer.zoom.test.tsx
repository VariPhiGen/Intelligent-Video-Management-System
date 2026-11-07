/**
 * Digital zoom must not move an annotation.
 *
 * THIS IS THE TEST THE ZOOM FEATURE EXISTS BEHIND. A box is stored in SOURCE
 * video pixels, and those pixels travel a long way from here: into the NVR's
 * sidecar, into the COCO export, and into the QA review payload as percentages
 * of the frame. Every one of those consumers takes the number on trust, and a
 * box that is off by the zoom factor still looks like a perfectly ordinary
 * box. There is no downstream check that would catch it — the first symptom
 * would be a training crop landing on empty tarmac weeks later.
 *
 * The invariant: the same drag over the same CONTENT yields the same source
 * box, whatever the zoom. The drags below are expressed in screen pixels, so
 * at 3x they are literally three times further apart — and must still save
 * byte-identical coordinates.
 *
 * What makes it true is that `point()` divides by `appliedScale()` (painted
 * width ÷ layout width) while `sourceBox()` measures the layout box. Change
 * either half alone and this test fails, which is the point: the two are only
 * correct together.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));

const uploads: FormData[] = [];
vi.mock('@/lib/api', () => ({
  apiUpload: (_url: string, form: FormData) => {
    uploads.push(form);
    return Promise.resolve({});
  },
}));

import { AnnotateLayer } from './AnnotateLayer';
import type { LabelClass } from '@/lib/labelClasses';

const CLASSES: LabelClass[] = [
  { key: 'forklift', label: 'Forklift', color: '#ff8800', sort_order: 0,
    active: true, description: null, annotation_count: 0 },
];

/** Layout size of the canvas and the video's intrinsic size. Deliberately
 *  different from each other so a confusion between element space and source
 *  space cannot pass by coincidence. */
const LAYOUT_W = 640, LAYOUT_H = 360;
const SRC_W = 1920, SRC_H = 1080;

function videoEl() {
  const v = document.createElement('video');
  Object.defineProperty(v, 'videoWidth', { value: SRC_W });
  Object.defineProperty(v, 'videoHeight', { value: SRC_H });
  return v;
}

/** jsdom gives no layout and no canvas, so both have to be supplied.
 *  `scale` stands in for the ancestor transform: the painted rect is that
 *  many times the layout box, exactly as a CSS `scale()` would make it. */
function stubCanvas(scale: number) {
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
  // The UNTRANSFORMED box — what clientWidth reports and what the coordinate
  // maths is defined in.
  vi.spyOn(HTMLCanvasElement.prototype, 'clientWidth', 'get')
    .mockReturnValue(LAYOUT_W);
  vi.spyOn(HTMLCanvasElement.prototype, 'clientHeight', 'get')
    .mockReturnValue(LAYOUT_H);
  // The PAINTED box — what an ancestor transform makes of it.
  const w = LAYOUT_W * scale, h = LAYOUT_H * scale;
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect')
    .mockReturnValue({ left: 0, top: 0, right: w, bottom: h, width: w, height: h,
                       x: 0, y: 0, toJSON: () => ({}) });
}

/** Drive a drag whose endpoints are given in CONTENT (layout) coordinates,
 *  converted to the screen positions they would occupy at this zoom. */
async function drawAndSave(scale: number,
                           from: [number, number], to: [number, number]) {
  const canvas = document.querySelector('.pb-annotate-canvas') as HTMLCanvasElement;
  (canvas as unknown as Record<string, unknown>).setPointerCapture = () => {};
  (canvas as unknown as Record<string, unknown>).releasePointerCapture = () => {};
  const at = ([x, y]: [number, number]) =>
    ({ clientX: x * scale, clientY: y * scale, pointerId: 1 });

  await act(async () => { fireEvent.pointerDown(canvas, at(from)); });
  await act(async () => {
    fireEvent.pointerMove(canvas, at(to));
    fireEvent.pointerUp(canvas, at(to));
  });
  await act(async () => { screen.getByRole('button', { name: /^save$/i }).click(); });
}

function savedBox(form: FormData) {
  return ['bbox_x', 'bbox_y', 'bbox_w', 'bbox_h']
    .map(k => Number(form.get(k)));
}

function mount() {
  const videoRef = { current: videoEl() };
  return render(
    <AnnotateLayer videoRef={videoRef} camera="gate-cam" classes={CLASSES}
                   epochOf={() => 1790000000} />,
  );
}

afterEach(() => {
  uploads.length = 0;
  vi.restoreAllMocks();
});

describe('annotation coordinates under digital zoom', () => {
  it('maps a 1x drag to source pixels through the contain transform', () => {
    // 640x360 element, 1920x1080 source: the aspect matches, so there are no
    // letterbox bars and the scale is a clean 3. Asserting the absolute
    // numbers once anchors the other tests — without this they could all agree
    // on the same wrong answer.
    stubCanvas(1);
    mount();
    return drawAndSave(1, [80, 60], [260, 240]).then(() => {
      expect(savedBox(uploads[0])).toEqual([240, 180, 540, 540]);
    });
  });

  it.each([2, 3, 8])('saves identical source pixels at %sx zoom', async (scale) => {
    stubCanvas(scale);
    mount();
    await drawAndSave(scale, [80, 60], [260, 240]);
    expect(savedBox(uploads[0])).toEqual([240, 180, 540, 540]);
  });

  it('is not accidentally passing because zoom is ignored', async () => {
    // The failure this guards against is `point()` losing its division: the
    // box would then be stored `scale` times too large. Driving the SCREEN
    // coordinates of a 1x drag while the canvas is painted at 3x must produce
    // a box three times SMALLER in content terms — proving the division is
    // real and directional, not a no-op.
    stubCanvas(3);
    mount();
    await drawAndSave(1, [80, 60], [260, 240]);   // screen px, not scaled up
    // Approximate: dividing by 3 and re-multiplying by the source scale leaves
    // float dust (80.00000000000001). The assertion is about the factor of 3,
    // not the last bit of a double.
    savedBox(uploads[0]).forEach((v, i) =>
      expect(v).toBeCloseTo([80, 60, 180, 180][i], 6));
  });

  it('clamps a box dragged beyond the frame while zoomed', async () => {
    // At 8x an operator can easily drag past the edge of the content. The box
    // must still be legal source pixels rather than running off the frame.
    stubCanvas(8);
    mount();
    await drawAndSave(8, [-50, -50], [LAYOUT_W + 99, LAYOUT_H + 99]);
    const [x, y, w, h] = savedBox(uploads[0]);
    expect(x).toBe(0);
    expect(y).toBe(0);
    expect(x + w).toBeLessThanOrEqual(SRC_W);
    expect(y + h).toBeLessThanOrEqual(SRC_H);
  });
});
