/**
 * No live-view surface may crop the picture.
 *
 * THE DEFECT THIS GUARDS, which shipped and was found by reading rather than by
 * anyone noticing. `.wall-cell` is a fixed `aspect-ratio: 16/9` box
 * (global.css), and the wall variant alone painted its `<video>` with
 * `objectFit: 'cover'`. A camera whose picture is not 16:9 — a 4:3 dome, a
 * corridor-mode portrait stream — was therefore SILENTLY CROPPED: the wall cut
 * the top and bottom off a scene an operator was actively watching, and the
 * result looked perfectly normal. Sharp, well framed, and missing the edges.
 *
 * WHY IT SURVIVED REVIEW. `contain` and `cover` render identically when the
 * source is exactly 16:9, and every camera on the appliance this was written
 * against was 16:9. The bug is invisible until the first 4:3 camera is
 * installed, at which point nothing reports it — which is why it needs a test
 * rather than a note.
 *
 * WHAT IS ASSERTED, and why it is the rule rather than the instance. The
 * product position is recorded and unambiguous: letterbox, no setting, "nobody
 * wants crop". So the assertion is over EVERY variant, not just the one that
 * was wrong — a future fourth variant that reaches for `cover` fails here on
 * the day it is added, which is the whole point of writing it as an invariant.
 */
import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// The element's geometry is under test, not HLS. `useHls` attaches a media
// source and starts timers; neither is needed to see how the video is painted.
vi.mock('./useHls', () => ({
  useHls: () => ({ videoRef: { current: null }, offline: false, variant: 'main' }),
}));

import { LiveVideo } from './LiveVideo';

const VARIANTS = ['tile', 'focus', 'wall'] as const;

function videoFor(variant: (typeof VARIANTS)[number]) {
  const { container } = render(<LiveVideo slug="cam-1" variant={variant} />);
  const el = container.querySelector('video');
  expect(el, `the ${variant} variant rendered no <video>`).not.toBeNull();
  return el as HTMLVideoElement;
}

describe('the live view letterboxes rather than crops', () => {
  it.each(VARIANTS)('the %s variant never crops the picture', (variant) => {
    expect(videoFor(variant).style.objectFit).not.toBe('cover');
  });

  it.each(VARIANTS)('the %s variant letterboxes explicitly', (variant) => {
    // Stated positively as well as negatively: leaving objectFit unset would
    // pass the assertion above and fall back to the CSS default (`fill`),
    // which stretches — a different way to show the operator a wrong picture.
    expect(videoFor(variant).style.objectFit).toBe('contain');
  });

  it('paints the wall the same way as every other surface', () => {
    // The wall was the outlier, and an outlier is how this returns. If the
    // variants ever disagree again it should fail here, whichever one moved.
    const fits = new Set(VARIANTS.map(v => videoFor(v).style.objectFit));
    expect(fits.size, `variants disagree on how to fit the picture: ${[...fits]}`).toBe(1);
  });

  it('fills the cell it is given, so letterboxing is the only source of bars', () => {
    // `contain` only behaves if the element itself spans the cell; a video
    // sized smaller would letterbox for the wrong reason and mask a regression.
    const wall = videoFor('wall');
    expect(wall.style.width).toBe('100%');
    expect(wall.style.height).toBe('100%');
  });
});
