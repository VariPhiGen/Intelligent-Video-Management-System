/**
 * HomeAssistantPanel — connect the VMS to Home Assistant, and bring its
 * devices in as peripherals.
 *
 * THE POINT OF HA, for someone adding devices: a Shelly relay, a Hue bulb, a
 * Nuki lock or a Zigbee siren is added in HA by clicking Configure — no YAML,
 * no driver written by us. This screen then lists what HA can drive and turns
 * the ticked ones into ordinary peripherals, bound and verified, which the
 * rule builder offers like any other device.
 *
 * Two authorities, as on the API:
 *   · the CONNECTION (URL + long-lived token) is admin-only — the token is
 *     owner-level access to the whole of HA;
 *   · the IMPORT is peripheral_manage, like the rest of the inventory.
 *
 * The token is write-only. It is never shown, and a blank token field on save
 * means "keep the one stored".
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { useToast } from '@/components/Toast';
import { useAuth } from '@/lib/auth';
import {
  addDeviceUrl, browserUrl, connectionLabel, forgetConnection, getConnection,
  importEntities, importResultLabel, listEntities, saveConnection, testConnection,
  type HaConnection, type HaEntity,
} from '@/lib/homeassistant';
import { CATEGORY_ORDER, type DeviceCategory } from './peripheralsData';

function tone(state: string): string {
  return state === 'connected' ? 'green' : state === 'not_configured' ? 'dim' : 'red';
}

// ── Connection (admin) ───────────────────────────────────────────────────────

function ConnectionCard({ conn, onChanged }: {
  conn: HaConnection | null; onChanged: (c: HaConnection) => void;
}) {
  const toast = useToast();
  const [url, setUrl] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [seeded, setSeeded] = useState(false);
  if (conn && !seeded) {
    setSeeded(true);
    setUrl(conn.url || 'http://127.0.0.1:8123');
  }

  async function run(what: 'test' | 'save' | 'forget') {
    setBusy(true);
    try {
      if (what === 'test') {
        const r = await testConnection(url.trim(), token.trim() || undefined);
        toast(r.state === 'connected' ? 'Home Assistant answered with this token'
          : connectionLabel(r), r.state === 'connected' ? 'ok' : 'err');
      } else if (what === 'save') {
        const c = await saveConnection(url.trim(), token.trim() || undefined);
        setToken('');
        onChanged(c);
        toast(c.state === 'connected' ? 'Saved — connected to Home Assistant'
          : `Saved, but ${connectionLabel(c).toLowerCase()}`, c.state === 'connected' ? 'ok' : 'err');
      } else {
        if (!confirm('Forget the saved Home Assistant connection? Devices imported from it stop working until it is set again.')) return;
        const c = await forgetConnection();
        onChanged(c);
        toast('Saved connection removed');
      }
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  const needsToken = !conn || conn.source !== 'ui';
  return (
    <div className="panel">
      <div className="panel-head">
        <div className="card-title">Connection</div>
        {conn && (
          <span className="dotlabel" title={conn.error ?? undefined}>
            <span className="d" style={{ background: `var(--${tone(conn.state)})` }} />
            {connectionLabel(conn)}
          </span>
        )}
      </div>
      <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {conn?.source === 'env' && (
          <div className="banner banner-warn">
            <b>Set from the server's environment</b>
            <span className="banner-detail">
              HA_URL / HA_TOKEN in .env. Saving here takes over from them, and the
              token is then stored encrypted instead of in a plain file.
            </span>
          </div>
        )}
        <label style={{ display: 'block' }}>
          <div className="lbl">Home Assistant URL, as this server reaches it</div>
          <input value={url} onChange={e => setUrl(e.target.value)} style={{ width: '100%' }}
                 placeholder="http://127.0.0.1:8123" />
        </label>
        <label style={{ display: 'block' }}>
          <div className="lbl">Long-lived access token</div>
          <input type="password" autoComplete="off" value={token}
                 onChange={e => setToken(e.target.value)} style={{ width: '100%' }}
                 placeholder={conn?.token_set && conn.source === 'ui'
                   ? 'Stored — leave blank to keep it' : 'Paste the token from Home Assistant'} />
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>
            In Home Assistant: your profile (bottom left) → Security → Long-lived access
            tokens → Create token. It is stored encrypted and never shown again.
          </div>
        </label>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <div>
            {conn?.source === 'ui' && (
              <button className="btn-danger btn-sm" disabled={busy} onClick={() => run('forget')}>
                Forget
              </button>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn-subtle btn-sm" disabled={busy || !url.trim()
                      || (needsToken && !token.trim() && !conn?.token_set)}
                    onClick={() => run('test')}>Test</button>
            <button className="btn-primary btn-sm"
                    disabled={busy || !url.trim() || (needsToken && !token.trim())}
                    title={needsToken && !token.trim() ? 'A token is needed the first time' : undefined}
                    onClick={() => run('save')}>
              {busy ? 'Working…' : 'Save'}
            </button>
          </div>
        </div>
        {conn?.updated_at && conn.source === 'ui' && (
          <div style={{ fontSize: 11, color: 'var(--dim)' }}>
            Saved {new Date(conn.updated_at).toLocaleString()}
            {conn.updated_by ? ` by ${conn.updated_by}` : ''}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Import (peripheral_manage) ───────────────────────────────────────────────

interface Choice { checked: boolean; name: string; category: string }

function ImportCard({ haLink, onImported }: {
  haLink: string | null; onImported: () => void;
}) {
  const toast = useToast();
  const [entities, setEntities] = useState<HaEntity[] | null>(null);
  const [error, setError] = useState('');
  const [picks, setPicks] = useState<Record<string, Choice>>({});
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError('');
    try {
      const rows = await listEntities();
      setEntities(rows);
      setPicks(prev => Object.fromEntries(rows.map(e => [e.entity_id, prev[e.entity_id] ?? {
        checked: false, name: e.name, category: e.category ?? 'Access control',
      }])));
    } catch (e: any) {
      setEntities(null);
      setError(e.message || 'Could not ask Home Assistant');
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return (entities ?? []).filter(e => !f || e.entity_id.includes(f)
      || e.name.toLowerCase().includes(f));
  }, [entities, filter]);
  const chosen = Object.entries(picks).filter(([, p]) => p.checked);

  const set = (id: string, patch: Partial<Choice>) =>
    setPicks(p => ({ ...p, [id]: { ...p[id], ...patch } }));

  async function doImport() {
    setBusy(true);
    try {
      const r = await importEntities(chosen.map(([entity_id, p]) => ({
        entity_id, name: p.name.trim() || undefined, category: p.category,
      })));
      const failed = r.results.filter(x => x.result !== 'imported');
      toast(`Imported ${r.imported} device${r.imported === 1 ? '' : 's'}`
        + (failed.length ? ` · ${failed.map(f => `${f.entity_id}: ${importResultLabel(f)}`).join('; ')}` : ''),
        failed.length && !r.imported ? 'err' : 'ok');
      setPicks(p => Object.fromEntries(Object.entries(p).map(([k, v]) => [k, { ...v, checked: false }])));
      await load();
      onImported();
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="card-title">Devices in Home Assistant</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {haLink && (
            <a className="btn-subtle btn-sm" href={addDeviceUrl(haLink)} target="_blank" rel="noreferrer"
               title="Opens Home Assistant's Devices & services page in a new tab">
              ＋ Add a device in Home Assistant ↗
            </a>
          )}
          <button className="btn-subtle btn-sm" disabled={busy} onClick={() => void load()}>Refresh</button>
          <button className="btn-primary btn-sm" disabled={busy || !chosen.length} onClick={doImport}>
            {busy ? 'Importing…' : `Import ${chosen.length || ''}`.trim()}
          </button>
        </div>
      </div>
      <div className="panel-body flush">
        {error ? (
          <div className="empty">{error}</div>
        ) : entities === null ? (
          <div className="empty"><span className="spinner" /></div>
        ) : entities.length === 0 ? (
          <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
            <div className="glyph">⌂</div>
            <h4>Nothing in Home Assistant the VMS can switch yet</h4>
            <p>
              Add a device in Home Assistant (Settings → Devices &amp; services → Add
              integration), then press Refresh. Lights, switches and relays, locks,
              covers and gates, sirens, speakers, scenes, scripts, buttons, fans and
              valves appear here.
            </p>
          </div>
        ) : (
          <>
            <div style={{ padding: 'var(--s3) var(--s4)' }}>
              <input value={filter} onChange={e => setFilter(e.target.value)}
                     placeholder="Filter by name or entity id" style={{ width: '100%' }} />
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 36 }} />
                    <th>Name in the VMS</th>
                    <th style={{ width: 230 }}>Home Assistant entity</th>
                    <th style={{ width: 90 }}>State</th>
                    <th style={{ width: 180 }}>Category</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map(e => {
                    const p = picks[e.entity_id];
                    if (!p) return null;
                    const done = !!e.bound_to;
                    return (
                      <tr key={e.entity_id} style={done ? { opacity: 0.55 } : undefined}>
                        <td>
                          <input type="checkbox" disabled={done || busy} checked={p.checked}
                                 aria-label={`Import ${e.name}`}
                                 onChange={ev => set(e.entity_id, { checked: ev.target.checked })} />
                        </td>
                        <td>
                          {done
                            ? <span title={`Already a peripheral: ${e.bound_to}`}>{e.name} · imported</span>
                            : <input value={p.name} maxLength={120} style={{ width: '100%' }}
                                     onChange={ev => set(e.entity_id, { name: ev.target.value })} />}
                        </td>
                        <td><code>{e.entity_id}</code></td>
                        <td style={{ color: e.state === 'unavailable' ? 'var(--red)' : undefined }}>
                          {e.state ?? '—'}
                        </td>
                        <td>
                          <select value={p.category} disabled={done || busy} style={{ width: '100%' }}
                                  onChange={ev => set(e.entity_id, { category: ev.target.value as DeviceCategory })}>
                            {CATEGORY_ORDER.map(c => <option key={c} value={c}>{c}</option>)}
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── The tab ──────────────────────────────────────────────────────────────────

export function HomeAssistantPanel({ onImported }: { onImported: () => void }) {
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.peripheral_manage === true;
  const [conn, setConn] = useState<HaConnection | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    getConnection().then(setConn).catch(() => setConn(null));
  }, [isAdmin]);

  const haLink = browserUrl(conn?.url ?? null, window.location.hostname);

  return (
    <div className="fade">
      <div className="page-head">
        <div className="page-head-l">
          <h1 style={{ fontSize: 20, marginBottom: 2 }}>Home Assistant</h1>
          <p className="lede">
            Add smart devices in Home Assistant — most need only a click, no
            configuration files — then import them here. Imported devices appear on the
            Status panel and in the Rule builder like any other peripheral. The VMS keeps
            the rules; Home Assistant only carries them out.
          </p>
        </div>
      </div>
      {isAdmin && <ConnectionCard conn={conn} onChanged={setConn} />}
      {canManage
        ? <ImportCard haLink={haLink} onImported={onImported} />
        : <div className="empty">You need the Manage peripherals permission to import devices.</div>}
    </div>
  );
}
