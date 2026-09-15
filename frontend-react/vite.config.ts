import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

// Air-gap rule: everything bundles locally — no runtime CDN imports anywhere.
// Dev-only: proxy API + auth to a running stack so `npm run dev` works against
// the real backend. Follows API_PORT (the same knob as .env) so `API_PORT=8097
// npm run dev` targets a backend on a non-default port; defaults to 8091.
const API_PORT = process.env.API_PORT || '8091';
const API_TARGET = `http://localhost:${API_PORT}`;

// ── Brand, resolved once at build time ───────────────────────────────────────
// The product name is per-deployment (white-label), so it cannot be a literal
// in the bundle. It is set ONCE here, from the environment, and reaches every
// consumer from this one place:
//
//   • the SPA          — via `define` below, read by components/Logo.tsx
//   • the <title> tag  — via the brandHtml() plugin below
//   • the login page   — NOT from here; Keycloak is a separate service reading
//                        the realm's own displayName. deploy/keycloak/
//                        sync-client.py sets that from the same BRAND_NAME on
//                        every `up`, which is what keeps the two in step.
//
// Defaults are the shipped name, so a bare `npm run build` still produces a
// coherent bundle with no env set.
const BRAND_NAME = process.env.VITE_BRAND_NAME || 'VMS';
const BRAND_SITE = process.env.VITE_BRAND_SITE || 'Local Site';
// Pre-existing knob (src/lib/legal.ts) that was never actually wired into the
// build — declared in code, but nothing ever set it, so every image shipped the
// <ORG>/<REPO> placeholder. Threading it through here fixes that too.
const SOURCE_URL = process.env.VITE_SOURCE_URL || '';

/** Substitutes %VITE_BRAND_NAME% in index.html.
 *
 *  Vite does this natively for vars it has loaded, but leaves the literal
 *  `%VITE_BRAND_NAME%` in the output when the var is unset — a visible
 *  placeholder in the browser tab. Doing it here guarantees the default
 *  applies, so the title can never ship un-substituted. */
function brandHtml(): Plugin {
  return {
    name: 'vms-brand-html',
    transformIndexHtml(html: string) {
      return html.replaceAll('%VITE_BRAND_NAME%', BRAND_NAME);
    },
  };
}

export default defineConfig({
  plugins: [react(), brandHtml()],
  // Injected rather than left to Vite's env loading: these must resolve to the
  // defaults above when nothing is set, which `import.meta.env` alone does not
  // guarantee (an unset VITE_ var is simply absent).
  define: {
    'import.meta.env.VITE_BRAND_NAME': JSON.stringify(BRAND_NAME),
    'import.meta.env.VITE_BRAND_SITE': JSON.stringify(BRAND_SITE),
    'import.meta.env.VITE_SOURCE_URL': JSON.stringify(SOURCE_URL),
  },
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': API_TARGET,
      '/health': API_TARGET,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
});
