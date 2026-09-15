/**
 * PlaybackPage.tsx — Playback & Cases, ported from the legacy page-recordings:
 * tabs (single | multi-camera | incident cases "soon"), the NVR camera list
 * (recLoad), the single timeline player (pb-view) and the synced multi-camera
 * player (pm-view). ?cam=<slug> deep-links straight into the single player.
 */
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiFetch } from '@/lib/api';
import { useCameras } from '@/lib/cameras';
import type { NvrCamera } from './coverage';
import { MultiPlayer } from './MultiPlayer';
import { SinglePlayer } from './SinglePlayer';

/** Legacy recFmtBytes (decimal units, unlike lib fmtBytes). */
function recFmtBytes(b: number | null | undefined): string {
  if (b == null) return '—';
  if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
  if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB';
  return Math.round(b / 1e3) + ' KB';
}
function recFmtTs(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : '—';
}

export function PlaybackPage() {
  const [params, setParams] = useSearchParams();
  const { cameras } = useCameras();
  const [tab, setTab] = useState<'single' | 'multi'>('single');
  const [selCam, setSelCam] = useState<string | null>(() => params.get('cam'));
  // Deep-link target (epoch seconds) — from a Smart Search hit's URL, or set
  // by openCam() when a find-similar result jumps within playback. SinglePlayer
  // is KEYED on (camera, startAt), so every change here is a remount and the
  // player's existing consume-once startAt path does the seek — one mechanism
  // for both the cross-page and the in-page jump, never two seek paths to drift.
  const [startAt, setStartAt] = useState<number | null>(() => {
    const t = Number(params.get('t'));
    return Number.isFinite(t) && t > 0 ? t : null;
  });
  const [nvrCams, setNvrCams] = useState<NvrCamera[]>([]);
  const [nvrErr, setNvrErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadNvr = useCallback(async () => {
    try {
      const data = await apiFetch<{ cameras: NvrCamera[] }>('/nvr/cameras');
      // The NVR lists RECORDING NAMES, and a camera with a sub track has two:
      // `<slug>` and `<slug>_sub`. The sub is one camera's low-resolution copy,
      // not a camera — listing it here would offer the operator a second entry
      // for the same view, named by raw slug, and playback picks the track by
      // itself anyway. Filtered on the name because the sub outlives its own
      // setting: footage recorded before it was switched off is still there,
      // and still must not appear as a camera.
      setNvrCams((data.cameras || []).filter(c => !c.name.endsWith('_sub')));
      setNvrErr(null);
    } catch (e: any) {
      setNvrErr(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadNvr(); }, [loadNvr]);

  const openCam = (name: string, t?: number | null) => {
    setTab('single');
    setSelCam(name);
    setStartAt(t ?? null);
    setParams(t != null ? { cam: name, t: String(Math.floor(t)) } : { cam: name },
              { replace: true });
  };
  const closeCam = () => {
    setSelCam(null);
    setStartAt(null);
    setParams({}, { replace: true });
    loadNvr();
  };
  const switchTab = (t: 'single' | 'multi') => {
    if (t === tab) return;
    setTab(t);
    // Legacy recTab: entering multi (or coming back) closes the single player.
    setSelCam(null);
    setParams({}, { replace: true });
    if (t === 'single') loadNvr();
  };

  const regName = (n: string) => cameras.find(c => c.slug === n)?.name || n;

  return (
    <div className="fade">
      {/* The page title lives in the topbar (matching Live View) — no duplicate
          heading above the tabs. */}
      <div className="tabs page-tabs">
        <div className={`tab${tab === 'single' ? ' active' : ''}`} onClick={() => switchTab('single')}>
          Playback
        </div>
        <div className={`tab${tab === 'multi' ? ' active' : ''}`} onClick={() => switchTab('multi')}>
          Multi-camera playback
        </div>
        {/* Roadmap tab — badge it visibly rather than hiding the status in a
            hover title, matching the Map and camera-config tab rows. */}
        <div className="tab disabled" title="Incident cases are not available yet">
          Incident cases<span className="nav-soon" style={{ marginLeft: 6 }}>soon</span>
        </div>
      </div>

      {tab === 'multi' && <MultiPlayer nvrCams={nvrCams} registry={cameras} />}

      {tab === 'single' && selCam && (
        <SinglePlayer key={`${selCam}:${startAt ?? ''}`}
                      camera={selCam} nvrCams={nvrCams} registry={cameras}
                      onSelectCamera={openCam} onOpenAt={openCam}
                      onClose={closeCam} startAt={startAt} />
      )}

      {tab === 'single' && !selCam && (
        <div className="panel">
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Recorded channels</div>
                <div className="panel-sub">Open a channel to scrub its timeline and export clips.</div>
              </div>
            </div>
            {!nvrErr && !loading && !!nvrCams.length && (
              <div className="panel-actions">
                <span className="badge badge-green">
                  <span className="badge-dot" />{nvrCams.filter(c => c.recording).length} recording
                </span>
              </div>
            )}
          </div>
          <div className="panel-body flush">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Camera</th><th style={{ width: 130 }}>Recording</th><th>Footage from</th>
                  <th>Latest</th><th style={{ width: 100 }}>Storage</th><th style={{ width: 130 }}></th>
                </tr>
              </thead>
              <tbody>
                {nvrErr && (
                  <tr><td colSpan={6} style={{ padding: 0 }}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">⚠</div>
                      <h4>NVR unavailable</h4>
                      <p>{nvrErr}</p>
                      <button className="btn-ghost btn-sm" onClick={loadNvr}>Retry</button>
                    </div>
                  </td></tr>
                )}
                {/* Skeleton rows hold the table's shape while the NVR responds. */}
                {!nvrErr && loading && [0, 1, 2].map(i => (
                  <tr key={i}>
                    <td><div className="skel-stack"><span className="skel" style={{ width: 120 }} /><span className="skel" style={{ width: 78, height: 9 }} /></div></td>
                    <td><span className="skel" style={{ width: 88, height: 18, borderRadius: 999 }} /></td>
                    <td><span className="skel" style={{ width: 130 }} /></td>
                    <td><span className="skel" style={{ width: 130 }} /></td>
                    <td><span className="skel" style={{ width: 54 }} /></td>
                    <td />
                  </tr>
                ))}
                {!nvrErr && !loading && !nvrCams.length && (
                  <tr><td colSpan={6} style={{ padding: 0 }}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">⏱</div>
                      <h4>No footage yet</h4>
                      <p>Enabled cameras sync into the recorder automatically — this usually takes under a minute.</p>
                      <button className="btn-ghost btn-sm" onClick={loadNvr}>Check again</button>
                    </div>
                  </td></tr>
                )}
                {!nvrErr && nvrCams.map(c => (
                  <tr key={c.name} className="inv-row" onClick={() => openCam(c.name)} title="Open playback">
                    <td>
                      <div style={{ fontWeight: 600 }}>{regName(c.name)}</div>
                      <code style={{ fontSize: 10.5, color: 'var(--dim)' }}>{c.name}</code>
                    </td>
                    <td>
                      {c.recording
                        ? <span className="badge badge-green badge-live"><span className="badge-dot" />Recording</span>
                        : <span className="badge badge-gray"><span className="badge-dot" />Stopped</span>}
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: 12.5 }}>{recFmtTs(c.earliest)}</td>
                    <td style={{ color: 'var(--muted)', fontSize: 12.5 }}>{recFmtTs(c.latest)}</td>
                    <td style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}>{recFmtBytes(c.storage_bytes)}</td>
                    <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }} onClick={e => e.stopPropagation()}>
                      <button className="btn-ghost btn-sm" onClick={() => openCam(c.name)}>▶ Playback</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          </div>
        </div>
      )}
    </div>
  );
}
