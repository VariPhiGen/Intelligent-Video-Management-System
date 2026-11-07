/**
 * The zoom transform's arithmetic, without a DOM.
 *
 * `clamp` and `zoomAbout` are exported precisely so they can be tested here:
 * both are the kind of geometry that looks obviously right and is off by a
 * factor somewhere, and both fail in ways an operator experiences as the
 * player "fighting" them — the frame sliding off the stage, or the thing they
 * pointed at running away as they zoom toward it.
 */
import { describe, expect, it } from 'vitest';

import { clamp, zoomAbout, IDENTITY, MAX_ZOOM, MIN_ZOOM } from './useVideoZoom';

const W = 800, H = 450;

describe('clamp', () => {
  it('pins the offset to zero at 1x', () => {
    // At 1x the content exactly fills the stage, so ANY offset would show
    // background at an edge.
    expect(clamp({ scale: 1, tx: -120, ty: 80 }, W, H))
      .toEqual({ scale: 1, tx: 0, ty: 0 });
  });

  it('allows exactly the slack the zoom creates', () => {
    // At 2x the content is twice the stage, so it may be dragged by up to one
    // stage-width before the far edge would come into view.
    expect(clamp({ scale: 2, tx: -W, ty: -H }, W, H))
      .toEqual({ scale: 2, tx: -W, ty: -H });
    expect(clamp({ scale: 2, tx: -W - 1, ty: -H - 1 }, W, H))
      .toEqual({ scale: 2, tx: -W, ty: -H });
  });

  it('refuses a positive offset, which would gap the top-left', () => {
    expect(clamp({ scale: 3, tx: 50, ty: 50 }, W, H))
      .toEqual({ scale: 3, tx: 0, ty: 0 });
  });

  it('holds the scale inside its bounds', () => {
    expect(clamp({ scale: 99, tx: 0, ty: 0 }, W, H).scale).toBe(MAX_ZOOM);
    expect(clamp({ scale: 0.1, tx: 0, ty: 0 }, W, H).scale).toBe(MIN_ZOOM);
  });

  it('survives a zero-sized stage rather than producing NaN', () => {
    // Before first layout the stage measures 0. A NaN offset here would be
    // written into a style string and blank the player.
    const t = clamp({ scale: 2, tx: -10, ty: -10 }, 0, 0);
    expect(Number.isFinite(t.tx) && Number.isFinite(t.ty)).toBe(true);
  });
});

describe('zoomAbout', () => {
  it('keeps the anchored point under the pointer', () => {
    // The property that makes wheel-zoom feel right: whatever content pixel is
    // under the cursor stays there as the scale changes.
    const px = 600, py = 300;
    const t = zoomAbout(IDENTITY, 2, px, py, W, H);
    // Content coordinate under the pointer, before and after.
    const before = (px - IDENTITY.tx) / IDENTITY.scale;
    const after = (px - t.tx) / t.scale;
    expect(after).toBeCloseTo(before, 6);
    expect((py - t.ty) / t.scale).toBeCloseTo((py - IDENTITY.ty) / IDENTITY.scale, 6);
  });

  it('stays anchored across a sequence of steps', () => {
    // One step can be right by luck; drift shows up when the output of each
    // step is the input of the next, which is what a scroll wheel does.
    const px = 240, py = 120;
    let t = IDENTITY;
    const target = (px - t.tx) / t.scale;
    for (let i = 0; i < 6; i++) t = zoomAbout(t, t.scale * 1.4, px, py, W, H);
    expect((px - t.tx) / t.scale).toBeCloseTo(target, 4);
  });

  it('cannot leave the frame while zooming out', () => {
    // Zooming out shrinks the slack, so an offset that was legal becomes
    // illegal — the clamp inside zoomAbout is what stops a gap appearing.
    const zoomedCorner = zoomAbout(IDENTITY, 8, W, H, W, H);
    const out = zoomAbout(zoomedCorner, 1.2, W, H, W, H);
    expect(out.tx).toBeGreaterThanOrEqual(-(out.scale - 1) * W);
    expect(out.ty).toBeGreaterThanOrEqual(-(out.scale - 1) * H);
    expect(out.tx).toBeLessThanOrEqual(0);
  });

  it('returns to a centred identity when zoomed fully out', () => {
    const t = zoomAbout({ scale: 4, tx: -300, ty: -200 }, 1, 0, 0, W, H);
    expect(t).toEqual({ scale: 1, tx: 0, ty: 0 });
  });

  it('will not exceed the maximum however hard it is pushed', () => {
    expect(zoomAbout(IDENTITY, 1000, 0, 0, W, H).scale).toBe(MAX_ZOOM);
  });
});
