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
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// The element's geometry is under test, not HLS. `useHls` attaches a media
// source and starts timers; neither is needed to see how the video is painted.
const feed = vi.hoisted(() => ({ offline: false, unsupportedCodec: false }));
vi.mock('./useHls', () => ({
  useHls: () => ({ videoRef: { current: null }, variant: 'main', ...feed }),
}));

afterEach(() => { feed.offline = false; feed.unsupportedCodec = false; });

import { LiveVideo, LvBadges } from './LiveVideo';
import type { Camera } from '@/lib/types';

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

describe('an offline tile says why', () => {
  it('reports a lost signal as a lost signal', () => {
    feed.offline = true;
    render(<LiveVideo slug="cam-1" variant="tile" />);
    expect(screen.getByText('Signal lost')).toBeInTheDocument();
  });

  it('names H.265 rather than a lost signal when the browser cannot decode it', () => {
    // The camera is streaming and recording; "Signal lost" sent the operator
    // looking for a network fault that did not exist.
    feed.offline = true;
    feed.unsupportedCodec = true;
    render(<LiveVideo slug="cam-1" variant="tile" />);
    expect(screen.queryByText('Signal lost')).not.toBeInTheDocument();
    expect(screen.getByText('H.265 — not playable in this browser')).toBeInTheDocument();
    expect(screen.getByText(/sub stream to H\.264/)).toBeInTheDocument();
  });

  it('keeps the wall cell to the short form', () => {
    feed.offline = true;
    feed.unsupportedCodec = true;
    render(<LiveVideo slug="cam-1" variant="wall" />);
    expect(screen.getByText('H.265 not supported')).toBeInTheDocument();
    expect(screen.queryByText(/sub stream to H\.264/)).not.toBeInTheDocument();
  });
});

describe('the status badges', () => {
  const cam = (health_status: string) => ({ health_status, recording: true } as Camera);

  it('does not call a camera that has not been probed yet offline', () => {
    // Every camera is 'unknown' for ~40 s after the api restarts; drawn as
    // OFFLINE, an upgrade turned the whole wall red while it was streaming.
    render(<LvBadges c={cam('unknown')} />);
    expect(screen.getByText('CHECKING')).toBeInTheDocument();
    expect(screen.queryByText('OFFLINE')).not.toBeInTheDocument();
  });

  it('still calls a disconnected camera offline', () => {
    render(<LvBadges c={cam('disconnected')} />);
    expect(screen.getByText('OFFLINE')).toBeInTheDocument();
  });

  it('shows LIVE and REC for a connected, recording camera', () => {
    render(<LvBadges c={cam('connected')} />);
    expect(screen.getByText('LIVE')).toBeInTheDocument();
    expect(screen.getByText('REC')).toBeInTheDocument();
  });
});
