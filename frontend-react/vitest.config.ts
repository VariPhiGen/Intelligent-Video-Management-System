/**
 * vitest.config.ts — test configuration, kept separate from vite.config.ts.
 *
 * Merged from the real build config rather than restated, so the `@/` alias and
 * the branding `define` block have exactly one definition. A test that resolved
 * `@/lib/auth` differently from the bundle would be testing a different module
 * graph than the one that ships.
 *
 * `environment: 'jsdom'` is set here rather than per-file: the three original
 * test files are pure logic and do not need a DOM, but they do not object to
 * one either, and a per-file docblock pragma is the kind of thing a new test
 * silently forgets.
 *
 * `globals` stays FALSE. The existing tests import `describe`/`it`/`expect`
 * from 'vitest' explicitly and that is the better habit — the import is what
 * tells a reader (and the typechecker) where these names come from.
 */
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config';

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: false,
      setupFiles: [fileURLToPath(new URL('./src/test/setup.ts', import.meta.url))],
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      // The app imports plain .css for layout only; parsing it per test file
      // costs time and can assert nothing, since jsdom does not lay out.
      css: false,
      restoreMocks: true,
    },
  }),
);
