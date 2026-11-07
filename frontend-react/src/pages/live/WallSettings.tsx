/**
 * WallSettings.tsx — the wall's ⚙ panel.
 *
 *  • THIS SCREEN (everyone): every control changes this browser's OVERRIDE of the admin's default,
 *    applied live so it can be tuned while looking at the TV. Overridden fields are marked
 *    "customised · reset"; "Reset this screen" drops them all and the screen follows the default
 *    again. Moving a control back to the default value also stops overriding it.
 *  • Admins: "Save as the default for everyone" publishes what this screen shows (including its
 *    cameras) as the default, after which this screen stops overriding — it follows the default
 *    like every other screen. Plus the client's logos (light + dark theme) and the wall title.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Modal } from '@/components/Modal';
import { apiFetch, apiUpload } from '@/lib/api';
import { DEFAULT_WALL_TITLE, logoFor, type Branding } from '@/lib/branding';
import { useToast } from '@/components/Toast';
import { themed, WALL_PREF_LIMITS, type Theme, type WallKey, type WallPrefs } from './wallPrefs';

// Common wall backgrounds: theme default, true black (OLED/LED walls), dark greys, a dark navy that
// reads as "branded" without fighting the video, and white. Anything else via the colour picker.
const BG_SWATCHES = ['', '#000000', '#0b0f14', '#1f2937', '#0b1f3a', '#f3f4f6', '#ffffff'];
const TITLE_SWATCHES = ['', '#ffffff', '#111827', '#f59e0b', '#38bdf8', '#ef4444'];

const fmt = (v: unknown) => (v === '' ? 'theme default' : typeof v === 'boolean' ? (v ? 'on' : 'off')
  : Array.isArray(v) ? `${v.length} cameras` : typeof v === 'number' ? `${v}px` : String(v));

function LogoSlot({ which, url, has, otherHas, busy, run }: {
which: Theme; url: string | null; has: boolean; otherHas: boolean; busy: boolean;
run: (fn: () => Promise<unknown>, ok: string) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const upload = (f: File | undefined) => {
    if (!f) return;
    const form = new FormData();
    form.append('file', f);
    run(() => apiUpload(`/branding/logo?theme=${which}`, form), `${which === 'dark' ? 'Dark' : 'Light'}-theme logo updated`);
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'flex-start' }}>
      <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{which === 'dark' ? 'Dark theme' : 'Light theme'}</div>
      <div className="wall-logo-preview" style={{ background: which === 'dark' ? '#0b0f14' : '#ffffff' }}>
        {has && url ? <img src={url} alt={`${which} theme logo`} />
          : <span style={{ color: which === 'dark' ? '#9ca3af' : '#6b7280' }}>
              {has ? '…' : otherHas ? `Uses the ${which === 'dark' ? 'light' : 'dark'} logo` : 'No logo'}
            </span>}
      </div>
      <input ref={ref} type="file" accept="image/png,image/jpeg,image/webp" hidden
             onChange={e => { upload(e.target.files?.[0]); e.target.value = ''; }} />
      <div style={{ display: 'flex', gap: 6 }}>
        <button className="btn-ghost btn-sm" disabled={busy} onClick={() => ref.current?.click()}>
          {has ? 'Replace…' : 'Upload…'}
        </button>
        {has && (
          <button className="btn-ghost btn-sm" disabled={busy}
                  onClick={() => run(() => apiFetch(`/branding/logo?theme=${which}`, { method: 'DELETE' }), 'Logo removed')}>
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

// Theme defaults the real wall falls back to (global.css --bg / --text / .wall-stage), so the preview
// shows "theme default" honestly rather than as an empty swatch.
const THEME_PAGE = { light: { bg: '#f6f5f2', text: '#1a1611', muted: '#6b6457' },
                     dark: { bg: '#0a0e13', text: '#f1f5f9', muted: '#8b98a9' } } as const;
const STAGE_DEFAULT = '#05070c';
const PREVIEW_SCALE = 0.3;   // px on the real wall → px in the preview (sizes stay in proportion to each other)

/**
 * A miniature of the wall for ONE theme, drawn from the same settings the wall uses: padding strip,
 * logo, title size + colour, the live pill, and six tiles (one offline — hidden or shown as on the
 * wall). Both themes render side by side so the colour you're not currently in is still visible.
 */
export function WallPreview({ prefs, theme, title, logoUrl, active }: {
  prefs: WallPrefs; theme: Theme; title: string; logoUrl: string | null; active: boolean;
}) {
  const { bg, titleColor } = themed(prefs, theme);
  const page = THEME_PAGE[theme];
  const S = PREVIEW_SCALE;
  const tiles = prefs.hideOffline ? 5 : 6;
  return (
    <div className={`wall-preview${active ? ' on' : ''}`}>
      <div className="wall-preview-cap">{theme === 'dark' ? 'Dark theme' : 'Light theme'}{active ? ' · current' : ''}</div>
      <div style={{ background: bg || page.bg, borderRadius: 6, padding: bg ? '0 5px 5px' : '0 0 5px', overflow: 'hidden' }}>
        {/* the top padding, hatched so "empty space for the TV's overlay" reads as deliberate */}
        <div className="wall-preview-pad" title={`${prefs.topPad}px top padding`}
             style={{ height: prefs.topPad * S, margin: bg ? '0 -5px 5px' : '0 0 5px', minHeight: 5,
                      ...(prefs.topPad ? {} : { background: 'none' }) }} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, padding: '0 4px 5px', minWidth: 0 }}>
          {logoUrl && <img src={logoUrl} alt="" style={{ height: Math.max(6, prefs.logoH * S), maxWidth: prefs.logoH * S * 5,
                                                          objectFit: 'contain' }} />}
          <span style={{ fontSize: Math.max(5, prefs.titlePx * S), fontWeight: 600, lineHeight: 1.1, whiteSpace: 'nowrap',
                         overflow: 'hidden', textOverflow: 'ellipsis', color: titleColor || page.text, minWidth: 0 }}>
            {title}
          </span>
          <span style={{ fontSize: 6, color: page.muted, border: `1px solid ${page.muted}55`, borderRadius: 99,
                         padding: '0 4px', whiteSpace: 'nowrap' }}>5 / 6 live</span>
        </div>
        <div style={{ background: bg ? 'transparent' : STAGE_DEFAULT, borderRadius: bg ? 0 : 4, padding: 4,
                      display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 3 }}>
          {Array.from({ length: tiles }, (_, i) => {
            const offline = i === 5;
            return (
              <div key={i} style={{ aspectRatio: '16 / 9', borderRadius: 2,
                                    background: offline ? '#1a1f26' : `linear-gradient(160deg, #3d4b57, #1c242d ${50 + i * 6}%)`,
                                    position: 'relative' }}>
                <span style={{ position: 'absolute', top: 2, right: 2, width: 3, height: 3, borderRadius: '50%',
                               background: offline ? '#ef4444' : '#22c55e' }} />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export function WallSettings({ open, isAdmin, branding, theme, logoLightUrl, logoDarkUrl, prefs, defaults, local,
                               onOverride, onReset, onPublish, onClose, onChanged }: {
  open: boolean;
  isAdmin: boolean;
  branding: Branding;
  theme: Theme;
  logoLightUrl: string | null;
  logoDarkUrl: string | null;
  prefs: WallPrefs;               // effective (default + overrides)
  defaults: WallPrefs;            // the admin default alone
  local: Partial<WallPrefs>;      // this browser's overrides
  onOverride: (patch: Partial<WallPrefs>) => void;
  onReset: (keys?: WallKey[]) => void;
  onPublish: () => Promise<void>;
  onClose: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [title, setTitle] = useState(branding.wall_title || '');
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setTitle(branding.wall_title || ''); }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const nCustom = Object.keys(local).filter(k => k !== 'cameraIds').length;   // cameras count once
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try { await fn(); toast(ok); onChanged(); }
    catch (e: any) { toast(e?.message || 'Could not save', 'err'); }
    finally { setBusy(false); }
  };

  // Plain render helpers, NOT components: a component declared inside render is a new type on every
  // render, so React would remount the range input mid-drag (each drag step re-renders via onOverride).
  /** One settings row: label, control, and a "customised · reset" marker when overridden here. */
  const row = (label: string, keys: WallKey[], children: ReactNode) => (
    <div className="wall-set-row" key={label}>
      <span>{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', minWidth: 0 }}>{children}</div>
      {keys.some(k => k in local)
        ? <button className="wall-cust" title={`This screen overrides the default (${keys.map(k => fmt(defaults[k])).join(', ')}) — click to follow the default again`}
                  onClick={() => onReset(keys)}>customised · reset</button>
        : <span />}
    </div>
  );
  const slider = (k: 'topPad' | 'logoH' | 'titlePx', label: string) => row(label, [k], (<>
      <input type="range" min={WALL_PREF_LIMITS[k][0]} max={WALL_PREF_LIMITS[k][1]} value={prefs[k]}
             style={{ flex: 1 }} onChange={e => onOverride({ [k]: +e.target.value })} />
      <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, minWidth: 44, textAlign: 'right' }}>{prefs[k]}px</span>
    </>));
  const colors = (k: 'bgLight' | 'bgDark' | 'titleColorLight' | 'titleColorDark', label: string, swatches: string[],
                  fallback: string) => row(label, [k], (<>
      {swatches.map(c => (
        <button key={c || 'default'} className={`wall-swatch${prefs[k] === c ? ' on' : ''}`}
                title={c || 'Theme default'} aria-label={c ? `${label} ${c}` : `${label}: theme default`}
                style={{ background: c || 'repeating-linear-gradient(45deg, #9ca3af 0 3px, transparent 3px 6px)' }}
                onClick={() => onOverride({ [k]: c })} />
      ))}
      <input type="color" value={prefs[k] || fallback} onChange={e => onOverride({ [k]: e.target.value })}
             title="Any colour" aria-label={`${label}: any colour`}
             style={{ width: 34, height: 26, padding: 0, border: 'none', background: 'none' }} />
    </>));

  return (
    <Modal open={open} title="Video wall settings" onClose={onClose} width={620}
           footer={(
             <div style={{ display: 'flex', gap: 8, alignItems: 'center', width: '100%' }}>
               <button className="btn-ghost btn-sm" disabled={!nCustom} onClick={() => onReset()}
                       title="Drop every customisation on this screen and follow the admin's default">
                 Reset this screen to default
               </button>
               <div style={{ flex: 1 }} />
               <button className="btn-ghost btn-sm" onClick={onClose}>Close</button>
               {isAdmin && (
                 <button className="btn-primary btn-sm" disabled={busy || !nCustom}
                         title="Make what this screen shows — cameras, offline handling, header and colours — the default on every screen"
                         onClick={() => run(onPublish, 'Saved as the default for everyone')}>
                   Save as default for everyone
                 </button>
               )}
             </div>
           )}>
      <div className="wall-scope">
        {nCustom
          ? <>This screen has <b>{nCustom}</b> customised setting{nCustom === 1 ? '' : 's'}; everything else follows the
              admin's default. Customisations stay in this browser until reset.</>
          : <>This screen follows the admin's default. Changes below apply to <b>this screen only</b>
              {isAdmin ? ' — then “Save as default for everyone” to roll them out.' : '.'}</>}
      </div>

      {/* Sticky so it stays in view while scrolling down to the colours. */}
      <div className="wall-preview-row">
        {(['light', 'dark'] as const).map(t => (
          <WallPreview key={t} prefs={prefs} theme={t} active={t === theme}
                       title={branding.wall_title || DEFAULT_WALL_TITLE}
                       logoUrl={logoFor(t, logoLightUrl, logoDarkUrl)} />
        ))}
      </div>

      {row('Cameras', ['followAll', 'cameraIds'], (
        <span style={{ fontSize: 12.5 }}>
          {prefs.followAll ? 'All cameras (new ones join automatically)' : `${prefs.cameraIds.length} selected`}
          <span style={{ color: 'var(--muted)' }}> · change with ＋ Select cameras</span>
        </span>
      ))}
      {row('Offline cameras', ['hideOffline'], (
        <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12.5 }}>
          <input type="checkbox" style={{ width: 'auto' }} checked={prefs.hideOffline}
                 onChange={e => onOverride({ hideOffline: e.target.checked })} />
          Hide from the wall (header shows “12 / 14 live”)
        </label>
      ))}
      {slider('topPad', 'Top padding')}
      {slider('logoH', 'Logo height')}
      {slider('titlePx', 'Title size')}

      <div className="label" style={{ margin: '14px 0 8px' }}>
        Colours <span style={{ textTransform: 'none', fontWeight: 400 }}>· now showing the {theme} theme</span>
      </div>
      {colors('bgLight', 'Light background', BG_SWATCHES, '#f3f4f6')}
      {colors('titleColorLight', 'Light title', TITLE_SWATCHES, '#111827')}
      {colors('bgDark', 'Dark background', BG_SWATCHES, '#05070c')}
      {colors('titleColorDark', 'Dark title', TITLE_SWATCHES, '#ffffff')}

      {isAdmin && (
        <>
          <div style={{ borderTop: '1px solid var(--border)', margin: '14px 0' }} />
          <div className="label" style={{ marginBottom: 4 }}>All screens (admin)</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 12 }}>
            Saved immediately for everyone. With only one logo uploaded it is used in both themes.
          </div>
          <div className="label" style={{ marginBottom: 6 }}>Wall title</div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
            <input value={title} maxLength={80} placeholder={DEFAULT_WALL_TITLE}
                   onChange={e => setTitle(e.target.value)} style={{ flex: 1 }} />
            <button className="btn-ghost btn-sm" disabled={busy || title === (branding.wall_title || '')}
                    onClick={() => run(() => apiFetch('/branding', { method: 'PUT', body: JSON.stringify({ wall_title: title }) }),
                                       'Title saved')}>
              Save title
            </button>
          </div>
          <div className="label" style={{ marginBottom: 6 }}>Client logo</div>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
            <LogoSlot which="light" url={logoLightUrl} has={branding.has_logo} otherHas={branding.has_logo_dark}
                      busy={busy} run={run} />
            <LogoSlot which="dark" url={logoDarkUrl} has={branding.has_logo_dark} otherHas={branding.has_logo}
                      busy={busy} run={run} />
          </div>
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>PNG, JPEG or WebP · up to 2 MB each</div>
        </>
      )}
    </Modal>
  );
}
