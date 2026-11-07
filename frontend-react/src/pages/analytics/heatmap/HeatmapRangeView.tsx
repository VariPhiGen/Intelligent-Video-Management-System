/**
 * HeatmapRangeView — one camera's heatmap for any period.
 *
 * Pick a camera, a From and a To (date and time). The VMS adds up every stored
 * 5-minute heat block in that period and draws the total over a recent camera
 * frame. "Up to now" (the default, from today 00:00) also takes the block still
 * being filled, and the picture is redrawn every minute, so it grows through
 * the day.
 *
 * The period is answered in whole 5-minute blocks, so the line under the
 * picture says exactly what it covers rather than echoing what was asked for.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, apiBlob } from '@/lib/api';
import {
  RANGE_REFRESH_MS, fetchHeatmapAvailability, fetchHeatmapRange, heatmapFileName,
  heatmapRangeImagePath, saveObjectUrl, startOfToday, toInput,
  type HeatmapAvailability, type HeatmapCamera, type HeatmapRange,
} from '@/lib/heatmaps';
import { HeatmapDateTimeField } from './HeatmapDateTimeField';
import { Modal } from '@/components/Modal';
import { fmt } from './HeatmapCard';

const PRESETS = [
  { label: 'Today', from: (now: Date) => startOfToday(now) },
  { label: 'Last hour', from: (now: Date) => new Date(now.getTime() - 3_600_000) },
  { label: 'Last 24 hours', from: (now: Date) => new Date(now.getTime() - 86_400_000) },
] as const;

function mb(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function backgroundText(at: string | null): string {
  if (at === 'live') return 'the live camera frame';
  if (!at) return 'grey (no camera frame stored yet)';
  return `the latest camera frame, from ${fmt(at)}`;
}

export function HeatmapRangeView({ cameras, selected, onSelect }: {
  cameras: HeatmapCamera[];
  selected: string;
  onSelect: (cameraId: string) => void;
}) {
  const [from, setFrom] = useState(() => toInput(startOfToday()));
  const [to, setTo] = useState(() => toInput(new Date()));
  const [live, setLive] = useState(true);
  const [tick, setTick] = useState(0);
  const [info, setInfo] = useState<HeatmapRange | null>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'empty' | 'failed'>('loading');
  const [message, setMessage] = useState('');
  const [full, setFull] = useState(false);
  const [avail, setAvail] = useState<HeatmapAvailability | null>(null);
  /** The range the picture on screen was drawn for, for its file name. */
  const drawn = useRef<{ start: Date; end: Date } | null>(null);

  // Which days have heat. Refreshed with the picture, so a day the sweep has
  // just deleted greys out without a reload.
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    fetchHeatmapAvailability(selected)
      .then(a => { if (!cancelled) setAvail(a); })
      .catch(() => { if (!cancelled) setAvail(null); });
    return () => { cancelled = true; };
  }, [selected, tick]);
  const availableDays = useMemo(() => (avail ? new Set(avail.days) : null), [avail]);

  // Up to now: redraw every minute.
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setTick(t => t + 1), RANGE_REFRESH_MS);
    return () => clearInterval(timer);
  }, [live]);

  // The picture on screen stays until the next one arrives; its object URL is
  // released the moment it is replaced, and when the view goes away.
  const shown = useRef<string | null>(null);
  const show = (url: string | null) => {
    if (shown.current && shown.current !== url) URL.revokeObjectURL(shown.current);
    shown.current = url;
    setSrc(url);
  };
  useEffect(() => () => { if (shown.current) URL.revokeObjectURL(shown.current); }, []);

  // What to fetch. In "up to now" the To box only DISPLAYS the time, so a
  // minute rolling over cannot trigger a second fetch of the same picture.
  const until = live ? `live:${tick}` : to;

  useEffect(() => {
    if (!selected) return;
    const start = new Date(from);
    const end = live ? new Date() : new Date(to);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return;
    if (live) setTo(toInput(end));
    if (end <= start) {
      setState('failed'); setMessage('"To" must be after "From".'); return;
    }
    let cancelled = false;
    if (!shown.current) setState('loading');
    Promise.all([
      fetchHeatmapRange(selected, start, end),
      apiBlob(heatmapRangeImagePath(selected, start, end)).catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }),
    ]).then(([summary, blob]) => {
      if (cancelled) return;
      setInfo(summary);
      if (!blob) { show(null); setState('empty'); return; }
      drawn.current = { start, end };
      show(URL.createObjectURL(blob));
      setState('ready');
    }).catch((e: unknown) => {
      if (cancelled) return;
      setState('failed');
      setMessage(e instanceof ApiError ? e.message : String(e));
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, from, until]);

  const cam = cameras.find(c => c.camera_id === selected);
  const save = () => {
    if (src && drawn.current) {
      saveObjectUrl(src, heatmapFileName(cam?.name ?? 'camera', drawn.current.start, drawn.current.end));
    }
  };
  const covered = info?.covered_from
    ? `${fmt(info.covered_from)} → ${fmt(info.covered_to)}` : 'nothing stored in this period';

  return (
    <section className="panel hm-range">
      <div className="hm-controls hm-range-controls">
        <label>
          <span className="hm-k">Camera</span>
          <select aria-label="Camera" value={selected} onChange={e => onSelect(e.target.value)}>
            {cameras.map(c => <option key={c.camera_id} value={c.camera_id}>{c.name}</option>)}
          </select>
        </label>
        <div className="hm-field">
          <span className="hm-k">From</span>
          <HeatmapDateTimeField label="From" value={from} available={availableDays}
                                onChange={v => v && setFrom(v)} />
        </div>
        <div className="hm-field">
          <span className="hm-k">To</span>
          <HeatmapDateTimeField label="To" value={to} available={availableDays} disabled={live}
                                onChange={v => v && setTo(v)} />
        </div>
        <label className="hm-live-toggle">
          <span className="hm-k">Up to now</span>
          <input type="checkbox" aria-label="Up to now" checked={live}
                 onChange={e => setLive(e.target.checked)} />
        </label>
        <div className="ch-seg" role="group" aria-label="Quick ranges">
          {PRESETS.map(p => (
            <button key={p.label} onClick={() => {
              const now = new Date();
              setFrom(toInput(p.from(now))); setLive(true);
            }}>{p.label}</button>
          ))}
        </div>
      </div>

      <div className="panel-body flush">
        {state === 'failed' ? (
          <div className="hm-shot hm-shot-empty"><p>{message || 'The heatmap could not be drawn.'}</p></div>
        ) : state === 'empty' ? (
          <div className="hm-shot hm-shot-empty">
            <span className="glyph" aria-hidden="true">▦</span>
            <p>No heat or camera frame is stored for this period yet.</p>
          </div>
        ) : (
          <div className="hm-shot hm-range-shot">
            {src ? <img src={src} alt={`Heatmap for ${cam?.name ?? 'camera'}`}
                        onClick={() => setFull(true)} />
                 : <div className="skel hm-skel" />}
            {src && <button type="button" className="hm-save" onClick={save}
                            title="Save this heatmap, with its background, as a JPEG">
              ⤓ Download
            </button>}
          </div>
        )}
      </div>

      {(avail || (info && state !== 'failed')) && (
        <div className="hm-meta">
          {avail && (
            <>
              <div>
                <span className="hm-k">Heatmap available from</span>
                <span className="hm-v">{avail.first_at ? fmt(avail.first_at) : 'nothing stored yet'}</span>
              </div>
              <div>
                <span className="hm-k">Kept</span>
                <span className="hm-v">
                  {avail.retention_days} day{avail.retention_days === 1 ? '' : 's'} or {avail.storage_mb} MB,
                  whichever comes first · using {mb(avail.used_bytes)}
                </span>
              </div>
            </>
          )}
        </div>
      )}
      {info && state !== 'failed' && (
        <div className="hm-meta">
          <div><span className="hm-k">Heat covers</span><span className="hm-v">{covered}</span></div>
          <div>
            <span className="hm-k">Made from</span>
            <span className="hm-v">
              {info.blocks} five-minute block{info.blocks === 1 ? '' : 's'}
              {info.includes_live ? ' + the current one' : ''}
            </span>
          </div>
          <div><span className="hm-k">Drawn on</span><span className="hm-v">{backgroundText(info.background_at)}</span></div>
          <div className="hm-note">
            Colours are relative: red is the busiest spot in this period.
            {live ? ' Up to now: redrawn every minute.' : ''}
          </div>
        </div>
      )}

      <Modal open={full && !!src} title={cam?.name ?? ''} width="min(1200px, 94vw)"
             onClose={() => setFull(false)}>
        {src && <div className="hm-shot"><img src={src} alt={`Heatmap for ${cam?.name ?? 'camera'}`} />
          <button type="button" className="hm-save" onClick={save}>⤓ Download</button></div>}
        {info && <p className="hm-dim">{covered}</p>}
      </Modal>
    </section>
  );
}
