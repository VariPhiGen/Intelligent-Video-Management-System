/**
 * StorageTab.tsx — NVR disk usage + recorded-footage breakdown, ported from
 * the legacy admStorageInit / _stoBar. Camera slugs are resolved to registry
 * names via useCameras.
 *
 * The footage table also carries the per-camera PURGE (checkbox + bulk delete).
 * The legacy SPA had this as a 🗑 per row on the Playback page; the React port
 * dropped it, leaving DELETE /nvr/cameras/{slug}/recordings unreachable. It
 * lives here now because this is the screen where you look at what footage is
 * eating the disk. Admin-only — the api's /api/nvr proxy rejects non-GET from
 * anyone else, so a non-admin never sees the controls.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { useCameras } from '@/lib/cameras';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { foldPerCamera, storageRows } from './storageRows';

import {
  IconButton, PencilIcon, SplitBar, Stat, TuneIcon,
  daysLabel, fmtDays, gb, spanDays, splitOf, totalRange,
  type NvrHealth, type NvrStorage, type PerCameraUsage, type PurgeResult,
} from './storageBits';
import { BulkRetentionModal, PurgeModal, StorageCapModal } from './storageModals';

export function StorageTab() {
  const { cameras, refresh } = useCameras();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const [data, setData] = useState<{ nvr: NvrHealth | null; sto: NvrStorage | null } | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  // Bulk retention / grooming — a single modal editor for the whole fleet.
  const [bulkOpen, setBulkOpen] = useState(false);
  const [gRetention, setGRetention] = useState('');
  const [gGroom, setGGroom] = useState('');
  const [bulkTyped, setBulkTyped] = useState('');
  const [applying, setApplying] = useState(false);
  // Storage size cap — a single modal editor. '' = uncapped.
  const [capOpen, setCapOpen] = useState(false);
  const [capInput, setCapInput] = useState('');
  const [capBusy, setCapBusy] = useState(false);
  const [capTyped, setCapTyped] = useState('');

  const load = useCallback(async () => {
    const [nvr, sto] = await Promise.all([
      apiFetch<NvrHealth>('/nvr/health').catch(() => null),
      apiFetch<NvrStorage>('/nvr/storage').catch(() => null),
    ]);
    return { nvr, sto };
  }, []);

  useEffect(() => {
    let alive = true;
    load().then(d => { if (alive) setData(d); });
    return () => { alive = false; };
  }, [load]);

  if (!data) {
    return (
      <div className="wz-methods" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 16 }}>
        <div className="empty"><span className="spinner" /></div>
      </div>
    );
  }

  const { nvr, sto } = data;
  const disk = nvr ? nvr.disk : null;
  const diskCol = disk && disk.used_pct >= 95 ? 'var(--red)'
    : disk && disk.used_pct >= 90 ? 'var(--yellow)'
    : 'var(--accent)';

  // Per RECORDING NAME from the NVR, folded to one row per camera — see
  // storageRows.ts for why a sub is a portion of its parent rather than a row
  // of its own, and why that makes the purge's "freed" figure something this
  // component has to keep honest.
  const folded = foldPerCamera(sto?.per_camera);
  const subBytesOf = (slug: string) => folded.get(slug)?.sub ?? 0;
  const rows = storageRows(folded);

  const nameOf = (slug: string) => cameras.find(c => c.slug === slug)?.name || slug;
  // A camera can be deselected by a refresh that drops its row — only ever act
  // on selections that still exist in the current table.
  const selected = rows.filter(([slug]) => sel.has(slug));
  const selBytes = selected.reduce((a, [, b]) => a + b, 0);
  const allOn = rows.length > 0 && selected.length === rows.length;

  // ── Global apply: parse inputs, validate, and preview the fleet-wide impact ──
  const retentionDefault = cameras.find(c => c.retention_default_days != null)?.retention_default_days ?? 30;
  const groomDefault = sto?.groom_after_days_default ?? null;
  // Placeholder reflects the CURRENT groom window across the fleet (each camera's
  // override, or the appliance default when unset) so the admin sees what they're
  // about to override — not the bare appliance default. Collapses to one label,
  // or "mixed" when cameras disagree; falls back to the default when no cameras.
  const groomLabel = (v: number | null): string =>
    v == null ? 'appliance default' : v === 0 ? 'never compress' : `${v} day${v === 1 ? '' : 's'}`;
  const groomCurrent = [...new Set(cameras.map(c => c.groom_after_days ?? groomDefault))];
  const groomPlaceholder =
    groomCurrent.length === 1 ? `Current: ${groomLabel(groomCurrent[0])}`
    : groomCurrent.length > 1 ? 'Current: mixed'
    : groomDefault == null ? 'Appliance default'
    : groomDefault === 0 ? 'Default: never compress' : `Default: ${groomDefault} days`;
  // Retention placeholder mirrors the grooming one: show the CURRENT fleet-wide
  // window (each camera's override, or the appliance default when unset), or
  // "mixed" when cameras disagree — not the bare appliance default. Same
  // resolution as loweredRetentionCams below so preview and hint stay in step.
  const retCurrent = [...new Set(
    cameras.map(c => c.retention_days ?? c.retention_default_days ?? retentionDefault))];
  const retentionPlaceholder =
    retCurrent.length === 1 ? `Current: ${retCurrent[0]} day${retCurrent[0] === 1 ? '' : 's'}`
    : retCurrent.length > 1 ? 'Current: mixed'
    : `Default: ${retentionDefault} days`;
  const gRetVal = gRetention.trim() === '' ? null : Number(gRetention);
  const gGroomVal = gGroom.trim() === '' ? null : Number(gGroom);
  const validRet = gRetVal == null || (Number.isInteger(gRetVal) && gRetVal >= 1 && gRetVal <= 3650);
  const validGroom = gGroomVal == null || (Number.isInteger(gGroomVal) && gGroomVal >= 1 && gGroomVal <= 3650);
  const canApply = (gRetVal != null || gGroomVal != null) && validRet && validGroom && cameras.length > 0;
  // Cameras whose footage would be deleted (retention lowered below what they keep now).
  const loweredRetentionCams = gRetVal == null ? []
    : cameras.filter(c => gRetVal < (c.retention_days ?? c.retention_default_days ?? retentionDefault));
  // Cameras that would start grooming (compressing) sooner than they do now (0 = never).
  const groomsSoonerCams = gGroomVal == null ? []
    : cameras.filter(c => {
        const cur = c.groom_after_days ?? (groomDefault ?? 0);
        return gGroomVal < (cur > 0 ? cur : Infinity);
      });

  // Storage size cap editor: parse, validate, and flag a destructive lowering
  // (new cap below current usage → oldest footage deleted next pass).
  //
  // The upper bound is the disk, not a constant. A cap above what the volume can
  // hold is not a bigger limit — the NVR's disk-floor evictor reclaims space
  // before the cap is ever reached, so the number is displayed and never
  // enforced. The server rejects those with a 400; checking here too means the
  // operator sees the ceiling while typing instead of after saving.
  //
  // capMax falls back to the absolute bound when the NVR could not measure the
  // disk (max_limit_gb null), which is the same fail-open rule the API applies.
  // The cap chip's tone: red once it is actively evicting, amber as it closes
  // in. Thresholds come from the NVR (storage/alerts.py) rather than being
  // re-picked here, so the banner and the chip cannot disagree.
  const capTone = sto?.pressure?.cap_binding ? 'var(--red)'
    : (sto?.usage_pct ?? 0) >= 90 ? 'var(--yellow)' : 'var(--text)';
  const capMax = sto?.max_limit_gb ?? 1_000_000;
  const capGb = capInput.trim() === '' ? null : Number(capInput);
  const capInRange = capGb == null || (Number.isInteger(capGb) && capGb >= 0 && capGb <= 1_000_000);
  const capOverMax = capGb != null && capGb > capMax;
  const capValid = capInRange && !capOverMax;
  const capWillDelete = capGb != null && capGb > 0 && capGb < (sto?.total_gb ?? 0);

  function toggle(slug: string) {
    setSel(s => {
      const n = new Set(s);
      if (n.has(slug)) n.delete(slug); else n.add(slug);
      return n;
    });
  }
  function toggleAll() {
    setSel(allOn ? new Set() : new Set(rows.map(([slug]) => slug)));
  }

  async function purge() {
    setBusy(true);
    const targets = selected.map(([slug]) => slug);
    let ok = 0, freed = 0;
    const failed: string[] = [];
    // No bulk endpoint on the NVR — one DELETE per camera, sequential so a
    // 40-camera purge can't stampede the recorder's index lock.
    //
    // One DELETE per camera ROW, which is not the same as one recording track:
    // the API's /nvr proxy fans a whole-camera purge out to `<slug>_sub` too
    // and returns the merged byte total. That is why the "incl. N low-res" line
    // on the row above and the freed figure below now agree — before the
    // fan-out existed this loop deleted the main only, while the row counted
    // (and this toast claimed) the sub's bytes as well.
    for (const slug of targets) {
      try {
        const r = await apiFetch<PurgeResult>(`/nvr/cameras/${encodeURIComponent(slug)}/recordings`,
          { method: 'DELETE' });
        ok += 1;
        freed += r.bytes_freed || 0;
      } catch (e: any) {
        // 404 = the camera had nothing left to delete; that's the desired state.
        if (/no recordings/i.test(e.message || '')) ok += 1;
        else failed.push(nameOf(slug));
      }
    }
    setBusy(false);
    setConfirming(false);
    setTyped('');
    setSel(new Set());
    if (failed.length) {
      toast(`Purged ${ok} of ${targets.length} — failed: ${failed.join(', ')}`, 'err');
    } else {
      toast(`Purged ${ok} camera${ok === 1 ? '' : 's'} — ${gb(freed)} freed`);
    }
    setData(await load());
  }

  function openBulk() {
    setGRetention('');
    setGGroom('');
    setBulkTyped('');
    setBulkOpen(true);
  }

  // Push the same retention / grooming windows to every camera. No bulk NVR
  // endpoint, so — like purge — loop one PUT per camera; each fans out to the
  // NVR internally. Blank field = leave that setting untouched.
  async function applyGlobal() {
    const body: Record<string, number> = {};
    if (gRetention.trim() !== '') body.retention_days = Number(gRetention);
    if (gGroom.trim() !== '') body.groom_after_days = Number(gGroom);
    setApplying(true);
    let ok = 0;
    const failed: string[] = [];
    for (const c of cameras) {
      try {
        await apiFetch(`/cameras/${c.id}`, { method: 'PUT', body: JSON.stringify(body) });
        ok += 1;
      } catch {
        failed.push(c.name);
      }
    }
    setApplying(false);
    setBulkOpen(false);
    setBulkTyped('');
    setGRetention('');
    setGGroom('');
    if (failed.length) {
      toast(`Applied to ${ok} of ${cameras.length} — failed: ${failed.join(', ')}`, 'err');
    } else {
      toast(`Applied to all ${ok} camera${ok === 1 ? '' : 's'}`);
    }
    refresh();
    setData(await load());
  }

  function openCap() {
    setCapInput(sto?.limit_gb ? String(sto.limit_gb) : '');
    setCapTyped('');
    setCapOpen(true);
  }
  // Persist the global size cap. gb=0 (blank) = uncapped. Lowering it below
  // current usage deletes the oldest footage across all cameras next pass — the
  // modal gates that case behind a Type-DELETE field (capWillDelete).
  async function doSaveCap() {
    if (!capValid) return;
    const gb = capGb ?? 0;
    setCapBusy(true);
    try {
      await apiFetch(`/nvr/storage/limit?gb=${gb}`, { method: 'PUT' });
      toast(gb ? `Storage cap set to ${gb} GB` : 'Storage cap removed — uncapped');
      setCapOpen(false);
      setCapTyped('');
      setData(await load());
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setCapBusy(false);
    }
  }

  return (
    <div>
      {/* State before content. A binding cap is the case this exists for: nothing
          is broken, every meter reads normal, and the site is quietly keeping
          less footage than it was configured to. */}
      {sto?.pressure && sto.pressure.state !== 'ok' && (
        <div className={`banner ${sto.pressure.state === 'critical' ? 'banner-crit' : 'banner-warn'}`}
             role="status" style={{ marginBottom: 'var(--s4)' }}>
          <div>
            <b>{sto.pressure.state === 'critical' ? 'Storage critical' : 'Storage needs attention'}</b>
            {sto.pressure.reasons.length === 1 ? (
              <div className="banner-detail">{sto.pressure.reasons[0]}</div>
            ) : (
              <ul>{sto.pressure.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            )}
            {/* Only actionable when the cap is what is biting. A full disk is
                already being handled by eviction; a binding cap needs a human
                to decide between more disk and less retention. */}
            {sto.pressure.cap_binding && (
              <div className="banner-detail" style={{ marginTop: 6 }}>
                Raise the size cap, add storage, or lower retention so the policy
                matches what is actually kept.
              </div>
            )}
          </div>
        </div>
      )}

      {!nvr && !sto ? (
        <div className="wz-methods" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 16 }}>
          <div className="empty">NVR is unreachable</div>
        </div>
      ) : (
        <div className="wz-methods" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 16 }}>
          {/* Disk — header + usage meter + Used/Free/Total tiles */}
          <div className="wz-method" style={{ cursor: 'default' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10 }}>
              <b style={{ fontSize: 13.5 }}>
                Disk{' '}
                <span style={{ fontWeight: 400, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
                  {disk ? disk.path : '—'}
                </span>
              </b>
              {disk && (
                <span style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: '.06em', textTransform: 'uppercase',
                               color: disk.used_pct >= 95 ? 'var(--red)' : disk.used_pct >= 90 ? 'var(--yellow)' : 'var(--green)' }}>
                  {disk.state}
                </span>
              )}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 14 }}>
              <span className="meter-track" style={{ flex: 1, height: 7 }}>
                <span className="meter-fill" style={{ width: `${Math.min(100, disk?.used_pct ?? 0)}%`, background: diskCol }} />
              </span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>
                {disk ? `${disk.used_pct}%` : '—'}
              </span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 14, marginTop: 16 }}>
              <Stat label="Used" value={disk ? `${disk.used_gb} GB` : '—'} />
              <Stat label="Free" value={disk ? `${disk.free_gb} GB` : '—'} />
              <Stat label="Total" value={disk ? `${disk.total_gb} GB` : '—'} />
            </div>
            {/* The three tiles above are the VM's numbers, and on Docker Desktop
                the VM's disk is a vhdx that grows on demand toward a ~1 TB
                maximum on a host drive that is usually far smaller. Left
                unsaid, this panel reads "plenty of room" at the exact moment
                the host drive is about to fill — and the meter cannot warn,
                because used_pct is a percentage of the same inflated total.
                The honest figure (the host's real free space) is not knowable
                from inside the VM, so say what the number IS rather than
                invent a better one. */}
            {disk?.virtual_backing && (
              <div style={{ marginTop: 14, padding: '9px 11px', borderRadius: 6,
                            background: 'var(--yellow-bg, rgba(220,170,40,.10))',
                            border: '1px solid var(--yellow, #d8a83c)' }}>
                <b style={{ fontSize: 11.5, color: 'var(--yellow, #d8a83c)' }}>
                  Virtual disk — capacity above is not real host space
                </b>
                <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4, lineHeight: 1.45 }}>
                  This volume is a {disk.virtual_backing.toUpperCase()} virtual disk that expands
                  on demand up to {disk.total_gb} GB, stored as a file on a host drive that is
                  smaller and not visible from here. Treat Free as an upper bound only. The
                  usage meter and its 90%/95% warnings are measured against the virtual total,
                  so they will not fire before the host drive fills — set a size cap the host
                  can genuinely honour, or move the disk image to a larger drive.
                </div>
              </div>
            )}
          </div>

          {/* Recorded footage — header (with Size cap) + hero total + tiles */}
          <div className="wz-method" style={{ cursor: 'default' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <b style={{ fontSize: 13.5 }}>Recorded footage</b>
              {sto && (
                <span style={{ fontSize: 11.5, color: 'var(--muted)', display: 'inline-flex', alignItems: 'center', gap: 4, whiteSpace: 'nowrap' }}>
                  Size cap:{' '}
                  {/* Coloured like the disk meter beside it. This read as plain
                      muted text at every value, so 98% of the cap looked exactly
                      like 12% — the disk had three warning surfaces and the cap
                      had none. */}
                  <span style={{ color: capTone }}>
                    {sto.limit_gb ? `${sto.limit_gb} GB · ${sto.usage_pct}%` : 'Uncapped'}
                  </span>
                  {isAdmin && <IconButton label="Edit storage cap" onClick={openCap}><PencilIcon /></IconButton>}
                </span>
              )}
            </div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 20, fontWeight: 600, marginTop: 12 }}>
              {sto ? `${sto.total_gb} GB` : '—'}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 14, marginTop: 16 }}>
              <Stat label="Full quality" value={gb(sto?.normal_bytes ?? 0)} dot="var(--accent2)" />
              <Stat label="Cold" value={gb(sto?.cold_bytes ?? 0)} dot="var(--tl-footage)" />
              <Stat label="Span" value={sto && spanDays(totalRange(sto)) != null
                ? `${fmtDays(spanDays(totalRange(sto)))} days` : '—'} />
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      marginBottom: 12, gap: 10, flexWrap: 'wrap' }}>
          <div className="card-title" style={{ margin: 0 }}>Footage per camera</div>
          {isAdmin && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              {selected.length > 0 && (
                <>
                  <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                    {selected.length} selected · <span style={{ fontFamily: 'var(--mono)' }}>{gb(selBytes)}</span>
                  </span>
                  <button className="btn-ghost btn-sm" onClick={() => setSel(new Set())}>Clear</button>
                  <button className="btn-danger btn-sm" onClick={() => { setTyped(''); setConfirming(true); }}>
                    Delete footage
                  </button>
                </>
              )}
              <button className="btn-ghost btn-sm" onClick={openBulk} disabled={!cameras.length}
                title="Set retention & grooming for all cameras"
                style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <TuneIcon /> Retention &amp; grooming
              </button>
            </div>
          )}
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {isAdmin && (
                  <th style={{ width: 34 }}>
                    <input type="checkbox" style={{ width: 'auto' }} checked={allOn}
                      disabled={!rows.length} onChange={toggleAll} title="Select all" />
                  </th>
                )}
                <th>Camera</th><th>Footage</th>
                <th>Days</th>
                <th>Retention</th>
                <th>
                  Full quality <span style={{ color: 'var(--accent2)' }}>●</span> ·
                  Cold <span style={{ color: 'var(--tl-footage)' }}>●</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {!nvr && !sto && <tr><td colSpan={isAdmin ? 6 : 5} className="empty">—</td></tr>}
              {(nvr || sto) && !rows.length && (
                <tr><td colSpan={isAdmin ? 6 : 5} className="empty">No recorded footage yet</td></tr>
              )}
              {rows.map(([slug, bytes]) => {
                const reg = cameras.find(c => c.slug === slug);
                const { normal, cold } = splitOf(sto?.per_camera?.[slug], bytes);
                return (
                  <tr key={slug}>
                    {isAdmin && (
                      <td>
                        <input type="checkbox" style={{ width: 'auto' }}
                          checked={sel.has(slug)} onChange={() => toggle(slug)} />
                      </td>
                    )}
                    <td>
                      <b>{reg ? reg.name : slug}</b>{' '}
                      <span style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--dim)' }}>{slug}</span>
                    </td>
                    <td style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>
                      {gb(bytes)}
                      {/* Say what part of the total is the low-resolution copy,
                          rather than hiding it or listing it as another camera. */}
                      {subBytesOf(slug) > 0 && (
                        <div style={{ fontSize: 10.5, color: 'var(--dim)' }}
                             title="Low-resolution copy recorded for fast playback scrubbing">
                          incl. {gb(subBytesOf(slug))} low-res
                        </div>
                      )}
                    </td>
                    <td style={{ fontFamily: 'var(--mono)', fontSize: 12 }}
                        title={daysLabel(sto?.per_camera?.[slug]) ?? undefined}>
                      {fmtDays(spanDays(totalRange(sto?.per_camera?.[slug])))}
                    </td>
                    <td style={{ fontFamily: 'var(--mono)', fontSize: 12, color: reg?.retention_days ? undefined : 'var(--muted)' }}>
                      {reg
                        ? (reg.retention_days
                            ? `${reg.retention_days} d`
                            : reg.retention_default_days ? `default (${reg.retention_default_days} d)` : 'default')
                        : '—'}
                    </td>
                    <td>
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10 }}>
                        <SplitBar normal={normal} cold={cold} />
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
                          {gb(normal)} · {gb(cold)}
                        </span>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="d-hint" style={{ marginTop: 10 }}>
          <b>Cold</b> footage has been groomed to keyframes to reclaim space (plays back as a slideshow, still on
          the timeline). <b>Days</b> is the observed on-disk span, which the size cap can shorten below a camera's
          retention.{isAdmin && ' Deleting footage here is immediate and permanent.'}
        </div>
      </div>

      <PurgeModal {...{ confirming, setConfirming, selected, nameOf, selBytes, typed, setTyped, purge, busy }} />
      <BulkRetentionModal {...{
        bulkOpen, setBulkOpen, applying, gGroom, setGGroom, groomPlaceholder,
        gRetention, setGRetention, retentionPlaceholder, validRet, validGroom,
        loweredRetentionCams, gRetVal, groomsSoonerCams, bulkTyped, setBulkTyped,
        applyGlobal, canApply, cameras,
      }} />
      <StorageCapModal {...{
        capOpen, setCapOpen, capBusy, capInput, setCapInput, sto, capValid,
        capInRange, capOverMax, capMax,
        capWillDelete, capGb, capTyped, setCapTyped, doSaveCap,
      }} />
    </div>
  );
}
