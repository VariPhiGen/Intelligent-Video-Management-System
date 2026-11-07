/**
 * The pure math behind the two live-feed PTZ gestures: click-to-centre and
 * scroll-to-zoom (see PtzClickLayer in pages/live/PtzControl.tsx).
 *
 * Both bugs this covers were reported as "the camera moves erratically" —
 * neither was the ONVIF call or the gain, both were commands stacking because
 * nothing debounced repeated input. These tests pin the math those fixes
 * depend on; the debounce/poll-loop TIMING itself (waitForSettle) lives in an
 * async function driven by real network calls and isn't retested here,
 * matching this repo's existing frontend test style (pure logic extracted and
 * unit-tested, not rendered components or timers). axesSettled is the pure
 * decision waitForSettle polls against, so that half is covered.
 */
import { describe, expect, it } from 'vitest';
import { accumulatedZoomStep, axesSettled, boxToZoomMove, clickToRelativeMove } from './ptz';

const RECT = { left: 100, top: 50, width: 400, height: 300 };

describe('clickToRelativeMove', () => {
  it('maps dead-centre to a null move — a click within the dead zone', () => {
    expect(clickToRelativeMove(300, 200, RECT, 0.22)).toBeNull();
  });

  it('maps the right edge to full positive pan, top edge to full positive tilt', () => {
    const right = clickToRelativeMove(500, 200, RECT, 0.22);
    expect(right?.pan).toBeCloseTo(0.22, 4);
    expect(right?.tilt).toBeCloseTo(0, 4);

    const top = clickToRelativeMove(300, 50, RECT, 0.22);
    expect(top?.tilt).toBeCloseTo(0.22, 4);   // ONVIF tilt is +1 UP
  });

  it('maps the left/bottom edges to negative pan/tilt', () => {
    const left = clickToRelativeMove(100, 200, RECT, 0.22);
    expect(left?.pan).toBeCloseTo(-0.22, 4);

    const bottom = clickToRelativeMove(300, 350, RECT, 0.22);
    expect(bottom?.tilt).toBeCloseTo(-0.22, 4);
  });

  it('scales down by gain rather than sending the raw fraction', () => {
    // Raw fraction at this point is 0.5; CENTRE_GAIN-style under-shoot must
    // shrink it, not pass it through — a 1:1 click near the edge would throw
    // the camera most of the way round its axis (see CENTRE_GAIN's comment).
    const move = clickToRelativeMove(400, 200, RECT, 0.22);
    expect(move?.pan).toBeCloseTo(0.5 * 0.22, 4);
  });

  it('respects a custom dead zone', () => {
    // Just inside a wide dead zone: null. Just outside a narrow one: a move.
    expect(clickToRelativeMove(310, 200, RECT, 0.22, 0.1)).toBeNull();
    expect(clickToRelativeMove(310, 200, RECT, 0.22, 0.02)).not.toBeNull();
  });
});

describe('accumulatedZoomStep', () => {
  it('zooms in (positive) on a negative deltaY — scroll up', () => {
    expect(accumulatedZoomStep(-100, 0.0025, 0.3)).toBeGreaterThan(0);
  });

  it('zooms out (negative) on a positive deltaY — scroll down', () => {
    expect(accumulatedZoomStep(100, 0.0025, 0.3)).toBeLessThan(0);
  });

  it('clamps a large fling to max rather than sending it raw', () => {
    expect(accumulatedZoomStep(-100000, 0.0025, 0.3)).toBe(0.3);
    expect(accumulatedZoomStep(100000, 0.0025, 0.3)).toBe(-0.3);
  });

  it('floors jitter under minStep to exactly 0, not a near-zero step', () => {
    expect(accumulatedZoomStep(-1, 0.0025, 0.3)).toBe(0);
  });

  it('scales proportionally to the accumulated delta within bounds', () => {
    expect(accumulatedZoomStep(-40, 0.0025, 0.3)).toBeCloseTo(0.1, 4);
  });
});

describe('boxToZoomMove', () => {
  it('centres on the box midpoint, not either corner', () => {
    // Box spans the right half of the frame (x: 300..500, i.e. centre..right
    // edge) at full height — midpoint x-fraction is 0.5 (halfway to the edge).
    const move = boxToZoomMove(300, 50, 500, 350, RECT, 0.22, 0.5, 0.3);
    expect(move?.pan).toBeCloseTo(0.5 * 0.22, 4);
    expect(move?.tilt).toBeCloseTo(0, 4);   // full height -> vertically centred already
  });

  it('zooms in more for a smaller box', () => {
    const bigBox = boxToZoomMove(150, 100, 450, 300, RECT, 0.22, 0.5, 0.3);   // ~75% of frame
    const smallBox = boxToZoomMove(280, 175, 320, 225, RECT, 0.22, 0.5, 0.3); // ~12% of frame
    expect(bigBox?.zoom).toBeGreaterThan(0);
    expect(smallBox?.zoom).toBeGreaterThan(bigBox!.zoom);
  });

  it('sizes the zoom step to the SMALLER dimension, so the whole box fits', () => {
    // Box: x 105..485 (widthFrac = 380/400 = 0.95), y 100..250
    // (heightFrac = 150/300 = 0.5). Height is the constraining dimension, so
    // zoom must come from fitFrac=0.5, NOT widthFrac=0.95 — using the wider
    // dimension would let the top/bottom edges spill past frame bounds.
    // rawZoom = 1 - 0.5 = 0.5; zoom = min(0.3, 0.5 * 0.5) = 0.25.
    const wideShort = boxToZoomMove(105, 100, 485, 250, RECT, 0.22, 0.5, 0.3);
    expect(wideShort?.zoom).toBeCloseTo(0.25, 4);
  });

  it('clamps zoom to zoomMax on a small box rather than sending it raw', () => {
    // Box: 40x30px = widthFrac 0.1, heightFrac 0.1 — both comfortably above
    // the default minBoxFrac (0.03) so this isn't rejected as degenerate.
    // rawZoom = 1 - 0.1 = 0.9; 0.9 * 0.5 = 0.45, clamped to zoomMax = 0.3.
    const move = boxToZoomMove(280, 185, 320, 215, RECT, 0.22, 0.5, 0.3);
    expect(move?.zoom).toBe(0.3);
  });

  it('rejects a degenerate sliver box even if drawn across most of the frame', () => {
    // Spans 95% of frame width but only 1% of frame height (3px of 300) —
    // a near-straight-line drag, not an intentional framing box.
    expect(boxToZoomMove(110, 199, 490, 202, RECT, 0.22, 0.5, 0.3)).toBeNull();
  });

  it('rejects a box smaller than minBoxFrac in either dimension', () => {
    expect(boxToZoomMove(295, 195, 305, 300, RECT, 0.22, 0.5, 0.3, 0.1)).toBeNull();
  });
});

describe('axesSettled', () => {
  it('is not settled while a moved axis still reports MOVING', () => {
    expect(axesSettled({ moving_pan_tilt: 'MOVING', moving_zoom: 'IDLE' }, { panTilt: true })).toBe(false);
  });

  it('is settled once a moved axis stops reporting MOVING', () => {
    expect(axesSettled({ moving_pan_tilt: 'IDLE', moving_zoom: 'IDLE' }, { panTilt: true })).toBe(true);
    // A camera that can't tell is not the same as one that's finished moving,
    // but it's also not a reason to keep the operator locked out forever —
    // anything other than the literal "MOVING" string counts as settled.
    expect(axesSettled({ moving_pan_tilt: 'UNKNOWN', moving_zoom: 'IDLE' }, { panTilt: true })).toBe(true);
  });

  it('ignores an axis the caller never moved, even if the camera reports it moving', () => {
    // The click gesture only ever drives pan/tilt — a zoom motor left running
    // by something else (a preset tour, another operator) must not block it.
    expect(axesSettled({ moving_pan_tilt: 'IDLE', moving_zoom: 'MOVING' }, { panTilt: true })).toBe(true);
  });

  it('waits on zoom too when the caller says it moved zoom', () => {
    expect(axesSettled({ moving_pan_tilt: 'IDLE', moving_zoom: 'MOVING' }, { zoom: true })).toBe(false);
  });

  it('treats a missing status as not settled — keep polling rather than assume done', () => {
    expect(axesSettled(null, { panTilt: true })).toBe(false);
    expect(axesSettled(undefined, { panTilt: true })).toBe(false);
  });
});

import { boxToCentre, containRect, framePoint, positionLabel, viewLabel } from './ptz';

describe('click-to-centre measures against the PICTURE, not the letterboxed panel', () => {
  // A 16:9 camera in a 2000×800 panel: picture is 1422×800, with ~289 px black bars left and right.
  const panel = { left: 0, top: 0, width: 2000, height: 800 };
  const pic = containRect(panel, 1920, 1080);

  it('finds the picture inside the bars', () => {
    expect(pic.width).toBeCloseTo(1422.2, 0);
    expect(pic.left).toBeCloseTo(288.9, 0);
    expect(pic.height).toBe(800);
  });

  it('a click on the picture edge is the frame edge (the panel would have said 0.71)', () => {
    const right = pic.left + pic.width;
    expect(framePoint(right, 400, pic).x).toBeCloseTo(1, 3);
    expect(framePoint(right, 400, panel).x).toBeCloseTo(0.71, 2);
  });

  it('+y is up', () => {
    expect(framePoint(1000, 0, pic).y).toBe(1);
    expect(framePoint(1000, 800, pic).y).toBe(-1);
  });

  it('a drawn box zooms so its larger side fills the frame, centred on the box', () => {
    // right half of the picture, middle half of its height → half the frame each way → 2×
    const b = boxToCentre(pic.left + pic.width * 0.5, 200, pic.left + pic.width, 600, pic)!;
    expect(b.x).toBeCloseTo(0.5, 2);
    expect(b.y).toBeCloseTo(0, 2);
    expect(b.zoom_factor).toBeCloseTo(2, 2);
  });

  it('a sliver is not a box', () => {
    expect(boxToCentre(500, 400, 900, 405, pic)).toBeNull();
  });
});

describe('positionLabel', () => {
  const base = { preset_token: null, preset_name: null, pan: 0, tilt: 0, zoom: 0, since: '', checked_at: '', cause: null, actor: null };
  it('says where it is', () => {
    expect(positionLabel({ ...base, state: 'at_preset', preset_token: '1', preset_name: 'Gate' })).toBe('📍 Gate');
    expect(positionLabel({ ...base, state: 'moving' })).toBe('Moving…');
    expect(positionLabel({ ...base, state: 'off_preset' })).toBe('Off preset');
    expect(positionLabel(null)).toBe('');
  });
});

import { vi } from 'vitest';
import { PtzDriver, STEER_MS, magLabel, ptzApi, steerVector, zoomedSpeed } from './ptz';

describe('zoom-aware pad', () => {
  it('labels the magnification: one decimal below 10, whole above', () => {
    expect(magLabel(1)).toBe('1×');
    expect(magLabel(3.24)).toBe('3.2×');
    expect(magLabel(13.6)).toBe('14×');
    expect(magLabel(null)).toBe('');
  });

  it('divides pad speed by the magnification, with a floor', () => {
    expect(zoomedSpeed(0.6, null)).toBe(0.6);
    expect(zoomedSpeed(0.6, 0.8)).toBe(0.6);           // "wider than 1x" never speeds it up
    expect(zoomedSpeed(0.6, 4)).toBeCloseTo(0.15);
    expect(zoomedSpeed(0.6, 40)).toBe(0.03);           // floored: the slowest setting still moves
  });

  it('steers by where the pointer is: direction around the hub, speed by distance', () => {
    const r = 54;                                      // compact pad radius
    expect(steerVector(2, 2, r)).toBeNull();            // over the hub
    const right = steerVector(r * 0.67, 0, r)!;
    expect(right.pan).toBeCloseTo(1);
    expect(right.tilt).toBeCloseTo(0);
    const upSlow = steerVector(0, -r * 0.3, r)!;        // screen up = tilt up, closer in = slower
    expect(upSlow.tilt).toBeGreaterThan(0.3);
    expect(upSlow.tilt).toBeLessThan(0.6);
    expect(steerVector(r * 3, 0, r)!.pan).toBe(1);      // never faster than the arrow itself
  });
});

describe('PtzDriver.steer', () => {
  it('sends a changed direction at most every STEER_MS, not on every pointer move', async () => {
    vi.useFakeTimers();
    const move = vi.spyOn(ptzApi, 'move').mockResolvedValue(undefined as never);
    vi.spyOn(ptzApi, 'stop').mockResolvedValue(undefined as never);
    const d = new PtzDriver('cam', () => {});
    d.steer({ pan: 1, tilt: 0 });                       // starts the move
    for (let i = 0; i < 20; i++) d.steer({ pan: 1, tilt: i / 20 });   // a burst of pointer moves
    expect(move).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(STEER_MS + 1);
    d.steer({ pan: 0, tilt: 1 });
    expect(move).toHaveBeenCalledTimes(2);
    expect(move).toHaveBeenLastCalledWith('cam', { pan: 0, tilt: 1 });
    await d.stop();
    move.mockRestore();
    vi.useRealTimers();
  });
});

import { magAt, percentSteps, zoomRange, zoomText } from './ptz';

describe('zoom as people read it', () => {
  // alpha's i-PRO after calibration: 1x at 0, ~13.5x at 0.0234 (zoom range 0..0.09375)
  const cal = { points: [{ zoom: 0, pan_per_width: 0.147, tilt_per_height: 0.2 },
                         { zoom: 0.0234, pan_per_width: 0.0109, tilt_per_height: 0.0148 }] };

  it('shows magnification on a calibrated camera, capped past the last measured zoom', () => {
    expect(magAt(cal, 0)).toBeCloseTo(1);
    expect(magAt(cal, 0.0234)).toBeCloseTo(13.49, 1);
    expect(magAt(cal, 0.09375)).toBeCloseTo(13.49 * 2.5, 0);   // = server's _EXTRAPOLATE_MAX
    expect(zoomText(0.0234, cal, [0, 0.09375])).toBe('13×');
  });

  it('falls back to percent of the range, then the raw number', () => {
    expect(zoomText(0.0234, null, [0, 0.09375])).toBe('25%');
    expect(zoomText(0.4, null, null)).toBe('0.400');
    expect(zoomText(null, cal, [0, 1])).toBe('—');
  });

  it('uses the camera-reported zoom limits, else generic 0..1 for absolute zoom', () => {
    expect(zoomRange({ supported: true, ranges: { zoom: [0, 0.09375] } } as any)).toEqual([0, 0.09375]);
    expect(zoomRange({ supported: true, absolute: { pan_tilt: true, zoom: true } } as any)).toEqual([0, 1]);
    expect(zoomRange({ supported: true } as any)).toBeNull();
    expect(percentSteps([0, 0.09375]).map(s => s.label)).toEqual(['Wide', '25%', '50%', '75%', 'Tele']);
    expect(percentSteps([0, 0.09375])[4].zoom).toBe(0.09375);
  });
});

describe('viewLabel', () => {
  const at = { state: 'at_preset', preset_token: '1', preset_name: 'Gate', pan: 0, tilt: 0, zoom: 0, since: '', checked_at: '',
               cause: null, actor: null } as const;
  it('says whether the camera is confirmed at its preset', () => {
    expect(viewLabel({ state: 'preset', verified_by: 'picture' }, at)?.text).toBe('✓ view confirmed');
    expect(viewLabel({ state: 'preset', verified_by: 'position' }, at)?.text).toBe('✓ view confirmed (position)');
    expect(viewLabel({ state: 'none', reason: 'arriving' }, at)?.tone).toBe('wait');
    expect(viewLabel({ state: 'none', reason: "the picture didn't settle" }, at)).toMatchObject({ tone: 'warn', title: "the picture didn't settle" });
    expect(viewLabel({ state: 'none', reason: 'moving' }, { ...at, state: 'moving' })).toBeNull();
  });
});
