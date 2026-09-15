/**
 * StqcBadge — the first rendered-component test in this frontend.
 *
 * Chosen as the representative component because it is where the product's
 * honest-UI rule is visible in a single element. CONTRIBUTING.md states the
 * rule as product law: "This product never fabricates a reading: an unknown
 * state renders as unknown, never as a plausible value." A compliance badge is
 * the sharpest case of it — a camera nobody has checked must not look like a
 * camera that passed, and the component's own docstring says why the tone is
 * amber rather than grey ("an unchecked camera is an open question the operator
 * owns, and rendering it as neutral is how a fleet quietly stays unverified").
 *
 * The pure functions get direct tests; the component gets rendered, because
 * `stqcDisplay` returning the right tone proves nothing about whether the badge
 * puts that tone on the screen.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StqcBadge, daysUntil, stqcDisplay } from './StqcBadge';

/** An ISO date `days` from today, so these tests do not expire. */
function isoIn(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

const camera = (status: string, validUntil: string | null = null) =>
  ({ stqc_status: status, stqc_valid_until: validUntil }) as any;

describe('daysUntil', () => {
  it('counts whole calendar days, not elapsed hours', () => {
    expect(daysUntil(isoIn(10))).toBe(10);
    expect(daysUntil(isoIn(0))).toBe(0);
  });

  it('goes negative once the date is past', () => {
    expect(daysUntil(isoIn(-3))).toBe(-3);
  });
});

describe('stqcDisplay', () => {
  it('reads an unchecked camera as an open question, not as neutral', () => {
    const { label, tone } = stqcDisplay('unknown' as any, null);
    expect(label).toBe('Unverified');
    expect(tone).toBe('warn');
  });

  it('treats a lapsed certificate as expired rather than certified', () => {
    const { label, tone } = stqcDisplay('certified' as any, isoIn(-1));
    expect(label).toBe('Certificate expired');
    expect(tone).toBe('bad');
  });

  it('warns inside the expiry window', () => {
    expect(stqcDisplay('certified' as any, isoIn(30)).tone).toBe('warn');
  });

  it('is green only well before expiry', () => {
    expect(stqcDisplay('certified' as any, isoIn(200)).tone).toBe('ok');
  });

  it('does not claim a status the camera never asserted', () => {
    // Anything unrecognised falls through to unknown — never to a pass.
    expect(stqcDisplay('something-new' as any, null).label).toBe('Unverified');
  });
});

describe('<StqcBadge />', () => {
  it('renders the label for the camera it is given', () => {
    render(<StqcBadge camera={camera('not_certified')} />);
    expect(screen.getByText('Not certified')).toBeInTheDocument();
  });

  it('shows an unverified camera as unverified', () => {
    render(<StqcBadge camera={camera('unknown')} />);
    expect(screen.getByText('Unverified')).toBeInTheDocument();
  });

  it('treats a missing status as unverified rather than blank', () => {
    // A camera row that predates the STQC columns arrives with null here. It
    // must still say something honest instead of rendering an empty badge.
    render(<StqcBadge camera={camera(null as any)} />);
    expect(screen.getByText('Unverified')).toBeInTheDocument();
  });

  it('carries the reason in a title an operator can hover', () => {
    render(<StqcBadge camera={camera('unknown')} />);
    expect(screen.getByText('Unverified')).toHaveAttribute(
      'title', 'Nobody has checked this',
    );
  });

  it('shows the expiry detail only when asked', () => {
    const valid = isoIn(200);
    const { rerender } = render(<StqcBadge camera={camera('certified', valid)} />);
    expect(screen.queryByText(`Valid to ${valid}`)).not.toBeInTheDocument();

    rerender(<StqcBadge camera={camera('certified', valid)} showDetail />);
    expect(screen.getByText(`Valid to ${valid}`)).toBeInTheDocument();
  });

  it('renders an expired certificate as expired on screen', () => {
    render(<StqcBadge camera={camera('certified', isoIn(-1))} />);
    expect(screen.getByText('Certificate expired')).toBeInTheDocument();
    expect(screen.queryByText('Certified')).not.toBeInTheDocument();
  });
});
