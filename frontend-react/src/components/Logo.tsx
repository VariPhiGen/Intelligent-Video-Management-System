/**
 * Logo.tsx — the SINGLE source of truth for the brand mark + wordmark.
 *
 * The NAME and SITE are per-deployment (white-label): they come from
 * VITE_BRAND_NAME / VITE_BRAND_SITE, resolved once in vite.config.ts, which is
 * the single place to set them. Do not hardcode a product name here — the
 * defaults below are only what a build with no environment falls back to.
 *
 * Change the subtitle here (BRAND) and the logo IMAGES at
 * `src/assets/logo-light.svg` / `logo-dark.svg` (the shared masters), and every
 * in-app surface updates: the sidebar, the auth loader, and the top bar's
 * site chip.
 *
 * TO REBRAND, drop your own `logo-light.png` / `logo-dark.png` beside the SVG
 * masters — the two formats live side by side and only the `url()` extensions
 * decide which is live. Change them in `styles/global.css` (`.brand-mark`, two
 * places: the base rule and the dark override) and in the Keycloak theme's
 * `vms.css` (`.v-logo`, same two), then run `./scripts/sync-brand.sh`. The two
 * are NOT interchangeable in shape: the SVGs are square 48x48 and use
 * `--brand-aspect: 1` in both themes, so a wide wordmark also means setting
 * `--brand-aspect` to its true ratio on the dark override (and the matching
 * explicit width on the Keycloak `.v-logo`).
 *
 * Your own mark is yours; the AGPL grant on this code carries no trademark
 * rights in anyone else's.
 *
 * The mark is theme-aware: the two variants are swapped by the `.brand-mark` CSS
 * on `html[data-theme]` (see styles/global.css), so it can't be an <img> — it's
 * a background-image on a sized <span>. The Keycloak sign-in screen is a separate
 * service that needs its own copies of the images, so after editing a master run
 * `./scripts/sync-brand.sh` to propagate them there.
 */
import type { CSSProperties } from 'react';
import { Icon } from './Icon';

export const BRAND = {
  name: import.meta.env.VITE_BRAND_NAME || 'VMS',
  subtitle: 'Enterprise',
  /** The appliance/site label in the top bar. One deployment, one site — this
   *  is the installation's own name, not the product's. */
  site: import.meta.env.VITE_BRAND_SITE || 'Local Site',
  /** Glyph beside the site label. A character, not an image: it is drawn in
   *  --accent2 and has to inherit the text colour and size. */
  siteGlyph: '\u2b21',
};

/** The square brand badge — theme-aware logo, drawn via the .brand-mark CSS. */
export function BrandMark({ size = 30, title }: { size?: number; title?: string }) {
  return (
    <span className="brand-mark" title={title} role="img" aria-label={BRAND.name}
      style={{ '--brand-size': `${size}px` } as CSSProperties} />
  );
}

/** The top-bar site chip — which installation this console is pointed at.
 *  Lives here rather than inline in Shell so every brand string has one home. */
export function SiteChip({ site = BRAND.site }: { site?: string }) {
  return (
    <span className="site-chip">
      <span className="site-glyph"><Icon name="site" size={15} /></span>{site}
    </span>
  );
}

/** Mark + wordmark (name over subtitle). Pass subtitle={null} to hide the text. */
export function Logo({ subtitle = BRAND.subtitle, size = 30, style }: {
  subtitle?: string | null;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <div className="logo" style={style}>
      <BrandMark size={size} />
      <div>{BRAND.name}{subtitle && <span>{subtitle}</span>}</div>
    </div>
  );
}
