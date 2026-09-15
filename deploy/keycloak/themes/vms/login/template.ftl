<#--
  template.ftl — the split-screen login shell: brand panel left, sign-in right.

  This REPLACES the classic PatternFly layout rather than restyling it: the base
  template centres a single card, and no amount of CSS turns that into a
  two-column page with a brand narrative beside it.

  Every login-theme page (login, OTP, reset password, error, …) renders through
  this macro, and their inner markup still comes from Keycloak. So vms.css
  styles form elements BY ELEMENT/ID, not via PatternFly class names — an
  inherited page we never look at still comes out sane.
-->
<#macro registrationLayout displayInfo=false displayMessage=true displayRequiredFields=false showTitle=true showAnotherWayIfPresent=true>
<!DOCTYPE html>
<html<#if realm.internationalizationEnabled> lang="${locale.currentLanguageTag}"</#if>>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>${msg("loginTitle",(realm.displayName!''))}</title>
  <#if properties.styles?has_content>
    <#list properties.styles?split(' ') as style>
      <link href="${url.resourcesPath}/${style}" rel="stylesheet" />
    </#list>
  </#if>
  <#if properties.scripts?has_content>
    <#list properties.scripts?split(' ') as script>
      <script src="${url.resourcesPath}/${script}" type="text/javascript"></script>
    </#list>
  </#if>
</head>

<body class="v-body">
<div class="v-split">

  <#-- ── Brand panel ─────────────────────────────────────────────────────── -->
  <aside class="v-brand">
    <div class="v-brand-top">
      <#-- Accessible name follows the realm displayName, which sync-client.py
           sets from BRAND_NAME on every `up` — same source as the footer, so
           a white-labelled deployment does not announce the shipped default. -->
      <span class="v-logo" role="img" aria-label="${realm.displayName!'VMS'}"></span>
      <span class="v-logo-text">Enterprise VMS</span>
    </div>

    <div class="v-brand-mid">
      <div class="v-eyebrow">AI-native surveillance</div>
      <h1 class="v-headline">One console for thousands of cameras — search it like you&rsquo;d ask a colleague.</h1>
      <p class="v-lede">
        Self-hosted video management: live view, recording, playback and search across your cameras,
        in a single operator workspace.
      </p>
    </div>

    <div class="v-brand-foot">
      <span>v2.0</span><span class="v-dot">·</span>
      <span>${realm.displayName!'VMS'}</span><span class="v-dot">·</span>
      <span id="v-clock">--:--:--</span>
    </div>
  </aside>

  <#-- ── Sign-in panel ───────────────────────────────────────────────────── -->
  <main class="v-panel">
    <div class="v-card">

      <div class="v-card-top">
        <#-- Light/Dark toggle. Writes the vms-theme cookie so the dashboard
             picks up the same choice (js/theme.js). -->
        <button type="button" id="v-theme-toggle" class="v-theme-toggle"
                title="Toggle light / dark" aria-label="Toggle light or dark theme"
                onclick="vmsToggleTheme()">☾</button>

        <#if realm.internationalizationEnabled && locale.supported?size gt 1>
          <div class="v-locales">
            <#list locale.supported as l>
              <a class="v-locale<#if l.languageTag == locale.currentLanguageTag> active</#if>"
                 href="${l.url}" hreflang="${l.languageTag}">${l.label}</a>
            </#list>
          </div>
        </#if>
      </div>

      <#if showTitle>
        <h2 class="v-title"><#nested "header"></h2>
        <p class="v-subtitle">${msg("vSignInSub")}</p>
      </#if>

      <#-- Keycloak's feedback (bad password, account locked, …) -->
      <#if displayMessage && message?has_content && (message.type != 'warning' || !isAppInitiatedAction??)>
        <div class="v-alert v-alert-${message.type}">
          <span>${kcSanitize(message.summary)?no_esc}</span>
        </div>
      </#if>

      <#nested "form">

      <#if auth?has_content && auth.showTryAnotherWayLink() && showAnotherWayIfPresent>
        <form id="kc-select-try-another-way-form" action="${url.loginAction}" method="post">
          <input type="hidden" name="tryAnotherWay" value="on"/>
          <a href="#" class="v-link"
             onclick="document.forms['kc-select-try-another-way-form'].submit();return false;">
            ${msg("doTryAnotherWay")}
          </a>
        </form>
      </#if>

      <#if displayInfo>
        <div class="v-info"><#nested "info"></div>
      </#if>

      <#nested "socialProviders">

      <p class="v-legal">
        ${msg("vLegalLine1")}<br>
        ${msg("vLegalLine2")}
      </p>
    </div>
  </main>

</div>

<script>
  // The footer clock, matching the dashboard topbar. Purely decorative.
  (function () {
    var el = document.getElementById('v-clock');
    if (!el) return;
    var tick = function () {
      var d = new Date(), p = function (n) { return String(n).padStart(2, '0'); };
      el.textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    };
    tick();
    setInterval(tick, 1000);
  })();
</script>
</body>
</html>
</#macro>
