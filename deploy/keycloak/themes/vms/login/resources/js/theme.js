/* theme.js — light/dark for the login page, kept in sync with the dashboard.
 *
 * The theme lives in a `vms-theme` cookie (dark|light). Cookies are the one
 * channel that crosses the port boundary between this page (Keycloak) and the
 * dashboard SPA (a different origin), so the two stay consistent:
 *   • the dashboard writes the cookie when you toggle there;
 *   • this page reads it before first paint (no flash), and its own toggle
 *     writes it back, which the dashboard reads on next load.
 *
 * Default is LIGHT — the same default the dashboard uses — so the two never
 * disagree on a fresh visit. We deliberately do NOT follow the OS theme here,
 * because the dashboard doesn't either; matching each other matters more than
 * matching the OS.
 *
 * Loaded via `scripts=js/theme.js` in theme.properties, which the base template
 * emits as a <script> in <head>, so the head part runs before paint.
 */
(function () {
  function readCookie() {
    try {
      var m = document.cookie.match(/(?:^|;\s*)vms-theme=(dark|light)(?:;|$)/);
      return m ? m[1] : null;
    } catch (e) { return null; }
  }

  // Apply before paint. No cookie → light (the shared default), never the OS.
  var initial = readCookie() || 'light';
  document.documentElement.setAttribute('data-theme', initial);

  function current() {
    return document.documentElement.getAttribute('data-theme') || 'light';
  }
  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try {
      var secure = location.protocol === 'https:' ? '; Secure' : '';
      document.cookie = 'vms-theme=' + theme + '; path=/; max-age=31536000; SameSite=Lax' + secure;
    } catch (e) { /* cookies blocked — the choice just won't persist */ }
    syncButton();
  }
  function syncButton() {
    var btn = document.getElementById('v-theme-toggle');
    // Show the icon of what a click switches TO.
    if (btn) btn.textContent = current() === 'dark' ? '☀' : '☾';
  }

  // Exposed for the toggle button's onclick in template.ftl.
  window.vmsToggleTheme = function () {
    apply(current() === 'dark' ? 'light' : 'dark');
  };

  // The button lives in <body>, so label it once the DOM is ready.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', syncButton);
  } else {
    syncButton();
  }
})();
