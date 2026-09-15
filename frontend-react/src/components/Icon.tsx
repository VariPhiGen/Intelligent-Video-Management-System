/**
 * Icon.tsx — the console's line icons.
 *
 * One 24px grid, a 1.75 stroke and round joins, drawn as SVG so every icon takes
 * currentColor and stays crisp at any size. They replace the Unicode glyphs the
 * rail used to carry (▦ ✦ ⏱ ⬡), which rendered at different weights and
 * baselines in every font and read as placeholders.
 *
 * Decorative by default (aria-hidden): each one sits beside a text label, and
 * the label is what a screen reader should announce.
 */
import type { SVGProps } from 'react';

const PATHS = {
  live:        ['M4 4h7v7H4z', 'M13 4h7v7h-7z', 'M4 13h7v7H4z', 'M13 13h7v7h-7z'],
  search:      ['M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14z', 'M20 20l-4-4'],
  playback:    ['M3.5 12a8.5 8.5 0 1 0 2.5-6', 'M3.5 4v4.5H8', 'M12 8v4l3 2'],
  map:         ['M9 4L3.5 6v14L9 18l6 2 5.5-2V4L15 6 9 4z', 'M9 4v14', 'M15 6v14'],
  cameras:     ['M3 7h11a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z', 'M15 10.5l6-3.5v10l-6-3.5'],
  ai:          ['M4 8V5a1 1 0 0 1 1-1h3', 'M16 4h3a1 1 0 0 1 1 1v3', 'M20 16v3a1 1 0 0 1-1 1h-3', 'M8 20H5a1 1 0 0 1-1-1v-3', 'M9 9h6v6H9z'],
  admin:       ['M12 3l7 3v5c0 4.6-3 8.5-7 10-4-1.5-7-5.4-7-10V6l7-3z', 'M9.5 12l2 2 3.5-3.5'],
  evidence:    ['M4 5h5l2 2h9v12H4V5z', 'M8 12h8', 'M8 15.5h5'],
  peripherals: ['M9 3v5', 'M15 3v5', 'M7 8h10v3a5 5 0 0 1-10 0V8z', 'M12 16v5'],
  key:         ['M8 16a4 4 0 1 1 0-8 4 4 0 0 1 0 8z', 'M12 12h9', 'M18 12v3', 'M21 12v2'],
  logout:      ['M9 20H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h4', 'M16 16l4-4-4-4', 'M20 12H9'],
  sun:         ['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z', 'M12 2.5v2', 'M12 19.5v2', 'M5.3 5.3l1.4 1.4', 'M17.3 17.3l1.4 1.4', 'M2.5 12h2', 'M19.5 12h2', 'M5.3 18.7l1.4-1.4', 'M17.3 6.7l1.4-1.4'],
  moon:        ['M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z'],
  site:        ['M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z', 'M12 7.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z'],
  clock:       ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z', 'M12 7v5l3 2'],
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18, ...rest }: { name: IconName; size?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true" focusable="false" {...rest}>
      {PATHS[name].map(d => <path key={d} d={d} />)}
    </svg>
  );
}

/** The rail's icon for a route, or null for a route this set does not draw
 *  (an extension's own entry), which then keeps the glyph it registered. */
export function navIcon(to: string): IconName | null {
  const byRoute: Record<string, IconName> = {
    '/live': 'live', '/smartsearch': 'search', '/playback': 'playback', '/map': 'map',
    '/cameras': 'cameras', '/ai': 'ai', '/admin': 'admin', '/evidence': 'evidence',
    '/peripherals': 'peripherals',
  };
  return byRoute[to] ?? null;
}
