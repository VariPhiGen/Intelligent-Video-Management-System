/**
 * Licence identity shown in the UI.
 *
 * AGPL-3.0 §13 obliges an operator who runs a MODIFIED version over a network
 * to offer that version's source to its remote users. A hardcoded upstream URL
 * would therefore be wrong for anyone who forks: they must point at their own
 * source, not ours. Hence the build-time override — a downstream modifier is
 * rebuilding the bundle anyway, so this costs them one env var.
 *
 * §5(d) then requires downstream modifiers to preserve this notice, which is
 * only worth anything if it exists in the first place. It did not until
 * 2026-08-27: nothing in the SPA said what the software was or where its source
 * lived, so a remote user had no way to find out.
 */
export const LICENSE_NAME = 'AGPL-3.0';
export const LICENSE_URL = 'https://www.gnu.org/licenses/agpl-3.0.html';

// The fallback is UPSTREAM's repository (settled 2026-08-31). A fork running
// modified code must override it with VITE_SOURCE_URL at build time — under
// §13 their remote users are owed *their* source, and pointing at ours would
// misstate what is actually running.
export const SOURCE_URL =
  import.meta.env.VITE_SOURCE_URL
  || 'https://github.com/VariPhiGen/Intelligent-Video-Management-System';

/** Guard kept from the placeholder era: if this ever reads as a template value
 *  again (e.g. a bad build-arg substitution), the footer omits the link rather
 *  than shipping it broken. */
export const SOURCE_CONFIGURED = !SOURCE_URL.includes('<ORG>');
