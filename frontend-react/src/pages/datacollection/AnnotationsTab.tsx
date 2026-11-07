/**
 * AnnotationsTab — the boxes an operator drew, and what happens to them.
 *
 * Annotate used to be a one-way door: you could mark the thing that mattered
 * and never find it again. This is the read side, and two of its controls are
 * load-bearing rather than convenient.
 *
 * RELABEL, BECAUSE DELETE IS IRREVERSIBLE HERE. The frame was captured at
 * pointer-down and the footage behind it may already have aged out, so a
 * misclicked class cannot be fixed by redrawing. Without the dropdown on each
 * tile the only remedy would be destroying a sample that cannot be recreated.
 *
 * KEEP, BECAUSE THE DEFAULT IS DELETION. A loose annotation carries an expiry
 * (ANNOTATION_TTL_MINUTES) and is swept when it passes. Until 048 nothing in
 * the product could clear that deadline, so every box drawn was deleted within
 * days. Keeping asks for a reason on purpose: `posture.derived_stills` reports
 * how many stills are held with no deadline, and a number nobody has to
 * justify is a number nobody reads.
 *
 * Four things this screen learned the hard way.
 *
 *   * THE FRAMES ARE BEARER-GATED. `/annotations/{id}/image` is gated on
 *     `playback_search`, and a plain `<img src>` cannot carry an Authorization
 *     header — every thumbnail was a 401. AuthImg fetches the bytes and hands
 *     the tag an object URL, which is what the sibling DatasetDetail already
 *     does for its frame grid.
 *   * THE BOX IS DRAWN ON THE FRAME. This is a screen for reviewing boxes and
 *     it showed none: the stored frame is the FULL source frame, so without the
 *     overlay you are looking at a scene and guessing which thing was labelled.
 *     See boxStyle() for the letterbox arithmetic.
 *   * THE COUNTS INCLUDE ZERO. The strip exists to say which class is
 *     under-sampled, and /stats only reports classes that HAVE annotations — so
 *     the one class with nothing in it, the only one worth acting on, was the
 *     one that did not appear. The active taxonomy is merged in at zero.
 *   * THE CAMERA FILTER IS A DROPDOWN, and its options come from the
 *     annotations rather than from the camera registry. Those two sets diverge
 *     on purpose — 047 keeps `camera_slug` out of a foreign key so training
 *     material outlives a decommissioned camera — so the registry would offer
 *     cameras with nothing and hide ones that have samples. A free-text slug
 *     was worse than both: a typo is a filter that silently returns nothing.
 *   * A KEPT REASON IS NOT OPTIONAL. `window.prompt` returns '' for an empty
 *     submit, and the API reads `kept_reason or row.kept_reason` — so a blank
 *     reason cleared the expiry and left the justification null, which is
 *     exactly the unanswerable number 048 exists to prevent. The reason is now
 *     a required field in a real dialog.
 */
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useToast } from '@/components/Toast';
import { Modal } from '@/components/Modal';
import { AuthImg } from '@/components/AuthImage';
import { listLabelClasses, type LabelClass } from '@/lib/labelClasses';
import {
  annotationStats, deleteAnnotation, exportAnnotations,
  listAnnotations, updateAnnotation,
  type Annotation, type AnnotationStats,
} from '@/lib/annotations';

const PAGE = 60;
/** The tile's own aspect, matching .ann-frame img in global.css. */
const TILE_ASPECT = 16 / 9;

/** Kept, or counting down — the two must not read alike. */
export function keepState(a: Annotation): { text: string; cls: string } {
  if (a.dataset_id !== null) {
    return { text: `Kept · dataset #${a.dataset_id}`, cls: 'ann-keep-kept' };
  }
  if (!a.expires_at) {
    return { text: a.kept_reason ? `Kept · ${a.kept_reason}` : 'Kept',
             cls: 'ann-keep-kept' };
  }
  const days = Math.ceil((Date.parse(a.expires_at) - Date.now()) / 86_400_000);
  if (days <= 0) return { text: 'Expiring now', cls: 'ann-keep-soon' };
  if (days <= 2) {
    return { text: `Expires in ${days} day${days === 1 ? '' : 's'}`,
             cls: 'ann-keep-soon' };
  }
  return { text: `Expires in ${days} days`, cls: 'ann-keep-clock' };
}

/**
 * Place the bbox over a container of `tileAspect`, given a frame that is
 * painted with `object-fit: contain`.
 *
 * Contain letterboxes on whichever axis the frame is short of, so the painted
 * area is NOT the element and percentages of the element would put the box in
 * the wrong place on every camera whose aspect is not the tile's. Working it
 * out from the two aspect ratios avoids measuring the DOM, which would mean a
 * layout read per tile and a reflow on every grid resize.
 */
export function boxStyle(a: Annotation, tileAspect: number): CSSProperties {
  const fw = a.frame.width || 1;
  const fh = a.frame.height || 1;
  const frameAspect = fw / fh;
  let pw = 100, ph = 100, ox = 0, oy = 0;
  if (frameAspect > tileAspect) {          // bars top and bottom
    ph = (tileAspect / frameAspect) * 100;
    oy = (100 - ph) / 2;
  } else if (frameAspect < tileAspect) {   // bars left and right
    pw = (frameAspect / tileAspect) * 100;
    ox = (100 - pw) / 2;
  }
  return {
    left: `${ox + (a.bbox.x / fw) * pw}%`,
    top: `${oy + (a.bbox.y / fh) * ph}%`,
    width: `${(a.bbox.width / fw) * pw}%`,
    height: `${(a.bbox.height / fh) * ph}%`,
  };
}

/** The stored frame with the box on it. One component so the tile and the
 *  lightbox cannot drift apart about where the box goes. */
function Frame({ a, color, aspect, onClick }: {
  a: Annotation;
  color: string;
  aspect: number;
  onClick?: () => void;
}) {
  return (
    <div className="ann-frame" style={{ aspectRatio: String(aspect) }}>
      <AuthImg path={`/annotations/${a.id}/image`}
               alt={`${a.class_key} on ${a.camera_slug}`}
               onClick={onClick}
               style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
      <span className="ann-box" data-testid="ann-box"
            style={{ ...boxStyle(a, aspect), borderColor: color }}>
        <span className="ann-box-tag" style={{ background: color }}>
          {a.class_key}
        </span>
      </span>
    </div>
  );
}

export function AnnotationsTab() {
  const toast = useToast();
  const [stats, setStats] = useState<AnnotationStats | null>(null);
  const [rows, setRows] = useState<Annotation[]>([]);
  const [total, setTotal] = useState(0);
  const [classes, setClasses] = useState<LabelClass[]>([]);
  const [filter, setFilter] = useState({ camera: '', class_key: '' });
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [zoom, setZoom] = useState<number | null>(null);
  const [keepFor, setKeepFor] = useState<Annotation | null>(null);
  const [keepWhy, setKeepWhy] = useState('');
  const [delFor, setDelFor] = useState<Annotation | null>(null);

  const colorOf = useMemo(
    () => Object.fromEntries(classes.map(c => [c.key, c.color])),
    [classes]);

  // The taxonomy is administered elsewhere and does not change while this
  // screen is open, so it is fetched once rather than with every page.
  useEffect(() => {
    listLabelClasses().then(setClasses).catch(() => setClasses([]));
  }, []);

  const loadStats = useCallback(() => {
    annotationStats().then(setStats).catch(() => { /* counts are not the page */ });
  }, []);
  useEffect(() => { loadStats(); }, [loadStats]);

  const loadPage = useCallback(async () => {
    try {
      const params: Record<string, string> = {
        limit: String(PAGE), offset: String(offset),
      };
      if (filter.camera) params.camera = filter.camera;
      if (filter.class_key) params.class_key = filter.class_key;
      const page = await listAnnotations(params);
      setRows(page.annotations);
      setTotal(page.total);
    } catch (e: any) {
      toast(e?.message || 'Could not load annotations', 'err');
    } finally {
      setLoaded(true);
    }
  }, [filter.camera, filter.class_key, offset]);

  useEffect(() => { void loadPage(); }, [loadPage]);

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      toast(ok);
      await loadPage();
      loadStats();          // a relabel or a delete moves the counts
    } catch (e: any) {
      toast(e?.message || 'That did not work', 'err');
    } finally {
      setBusy(false);
    }
  }

  const active = classes.filter(c => c.active);

  // /stats reports only classes that HAVE annotations; the empty one is the
  // whole point of the strip, so the active taxonomy is merged in at zero.
  const counts = useMemo(() => {
    const by = { ...(stats?.by_class ?? {}) };
    for (const c of active) if (!(c.key in by)) by[c.key] = 0;
    return Object.entries(by).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [stats, active]);

  // Alphabetical rather than by count: this is a lookup ("show me dock-cam"),
  // not a ranking, and a list that reorders as annotations land is a list you
  // cannot learn the shape of.
  const cameras = useMemo(
    () => Object.entries(stats?.by_camera ?? {})
      .sort((a, b) => a[0].localeCompare(b[0])),
    [stats]);

  // A camera can drop out of by_camera while selected — its last annotation was
  // deleted, or it aged out — and a select holding a value with no option shows
  // blank and silently filters everything away. Fall back to the whole pile.
  useEffect(() => {
    if (filter.camera && stats && !(filter.camera in stats.by_camera)) {
      setFilter(f => ({ ...f, camera: '' }));
    }
  }, [stats, filter.camera]);

  const zoomed = zoom !== null ? rows[zoom] : null;

  // Arrow keys through the pile, Escape out. The lightbox is the review posture
  // — a wall of 60 tiles is scanned, but a decision is made one frame at a time.
  useEffect(() => {
    if (zoom === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setZoom(null);
      if (e.key === 'ArrowLeft') setZoom(z => (z === null ? z : Math.max(0, z - 1)));
      if (e.key === 'ArrowRight') {
        setZoom(z => (z === null ? z : Math.min(rows.length - 1, z + 1)));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [zoom, rows.length]);

  const askKeep = (a: Annotation) => { setKeepFor(a); setKeepWhy(''); };

  const confirmKeep = () => {
    const a = keepFor;
    const why = keepWhy.trim();
    if (!a || !why) return;
    setKeepFor(null);
    return act(() => updateAnnotation(a.id, { keep: true, kept_reason: why }),
               'Kept');
  };

  const confirmDelete = () => {
    const a = delFor;
    if (!a) return;
    setDelFor(null);
    return act(() => deleteAnnotation(a.id), 'Deleted');
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="panel-head-l">
          <div className="panel-title">Annotations</div>
          <div className="panel-sub">
            Boxes drawn on Playback. A loose annotation is deleted when its
            expiry passes — keep the ones worth training on.
          </div>
        </div>
      </div>

      <div className="panel-body">
        {/* Clicking a count filters to it: the strip says which class is thin,
            and the next thing you want is to look at what it has. */}
        <div className="ann-counts" data-testid="ann-counts">
          {counts.map(([key, n]) => {
            const on = filter.class_key === key;
            return (
              <button key={key} type="button"
                      className={`chip ann-count${on ? ' is-on' : ''}${n === 0 ? ' is-zero' : ''}`}
                      data-class-key={key}
                      aria-pressed={on}
                      title={n === 0
                        ? `No annotations name ${key} yet`
                        : `Show only ${key}`}
                      onClick={() => { setOffset(0);
                                       setFilter(f => ({ ...f,
                                         class_key: on ? '' : key })); }}>
                <span className="badge-dot"
                      style={{ background: colorOf[key] ?? 'var(--dim)' }} />
                {key}<b>{n}</b>
              </button>
            );
          })}
          <span className="ann-count-total">{stats?.total ?? 0} total</span>
        </div>

        <div className="ann-filters">
          {/* A dropdown, not a text box. The options come from /stats' by_camera
              — the cameras that actually HAVE annotations — so every option
              returns something and nothing that has samples is missing. See
              the stats endpoint for why the camera registry is the wrong
              source. A slug typed by hand is a filter that silently returns
              nothing, and it needed debouncing to avoid a request per
              keystroke; neither problem exists here. */}
          <select value={filter.camera} aria-label="Filter by camera"
                  onChange={e => { setOffset(0);
                                   setFilter(f => ({ ...f,
                                                     camera: e.target.value })); }}>
            <option value="">All cameras</option>
            {cameras.map(([slug, n]) => (
              <option key={slug} value={slug}>{slug} ({n})</option>
            ))}
          </select>
          <select value={filter.class_key} aria-label="Filter by class"
                  onChange={e => { setOffset(0);
                                   setFilter({ ...filter, class_key: e.target.value }); }}>
            <option value="">All classes</option>
            {classes.map(c => (
              <option key={c.key} value={c.key}>{c.label}</option>
            ))}
          </select>
          {(filter.camera || filter.class_key) && (
            <button className="btn-subtle btn-sm"
                    onClick={() => { setOffset(0);
                                     setFilter({ camera: '', class_key: '' }); }}>
              Clear filters
            </button>
          )}
          <span className="ann-spacer" />
          <button className="btn-subtle btn-sm" disabled={busy || total === 0}
                  title={total === 0 ? 'Nothing to export yet'
                                     : 'Download the current selection as COCO JSON'}
                  onClick={() => act(async () => {
                    const out = await exportAnnotations({
                      camera: filter.camera || null,
                      class_keys: filter.class_key ? [filter.class_key] : null,
                    });
                    // Hand the operator the file. Without this the document is
                    // built server-side and thrown away — the S3 path needs a
                    // bucket this appliance may not have.
                    const blob = new Blob([JSON.stringify(out.coco, null, 2)],
                                          { type: 'application/json' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'annotations.coco.json';
                    a.click();
                    URL.revokeObjectURL(url);
                  }, 'Export downloaded')}>
            Export COCO
          </button>
        </div>

        {!loaded ? (
          <div className="empty"><span className="spinner" /></div>
        ) : rows.length === 0 ? (
          <div className="emptystate">
            <h3>{filter.camera || filter.class_key
              ? 'Nothing matches those filters'
              : 'No annotations yet'}</h3>
            <p>
              {filter.camera || filter.class_key
                ? 'Clear the filters to see the whole pile.'
                : 'Open Playback, press Annotate, pick a class and draw a box '
                  + 'around what matters. What you save shows up here.'}
            </p>
          </div>
        ) : (
          <>
            <div className="ann-grid">
              {rows.map((a, i) => {
                const keep = keepState(a);
                const kept = !a.expires_at;
                const color = colorOf[a.class_key] ?? '#ff4444';
                return (
                  <figure key={a.id} className="ann-tile" data-ann-id={a.id}>
                    <Frame a={a} color={color} aspect={TILE_ASPECT}
                           onClick={() => setZoom(i)} />
                    <figcaption>
                      <select value={a.class_key} aria-label="Class"
                              disabled={busy}
                              onChange={e => act(
                                () => updateAnnotation(a.id,
                                                       { class_key: e.target.value }),
                                'Relabelled')}>
                        {active.map(c => (
                          <option key={c.key} value={c.key}>{c.label}</option>
                        ))}
                        {/* A retired class still has to render, or the tile
                            would silently show the wrong label. */}
                        {!active.some(c => c.key === a.class_key) && (
                          <option value={a.class_key}>{a.class_key} (retired)</option>
                        )}
                      </select>
                      <div className="ann-tile-row">
                        <span className="ann-tile-cam" title={a.camera_slug}>
                          {a.camera_slug}
                        </span>
                        <time className="ann-tile-time" dateTime={a.captured_at}>
                          {new Date(a.captured_at).toLocaleString()}
                        </time>
                      </div>
                      <span className={`ann-keep ${keep.cls}`} title={keep.text}>
                        {keep.text}
                      </span>
                      <div className="ann-tile-actions">
                        <button className="btn-ghost btn-sm" disabled={busy}
                                data-action={kept ? 'release' : 'keep'}
                                title={kept
                                  ? 'Put this back on the retention clock'
                                  : 'Hold this past its expiry, with a reason'}
                                onClick={() => (kept
                                  ? act(() => updateAnnotation(a.id, { keep: false }),
                                        'Back on the clock')
                                  : askKeep(a))}>
                          {kept ? 'Release' : 'Keep'}
                        </button>
                        <button className="btn-ghost btn-sm" disabled={busy}
                                data-action="delete"
                                onClick={() => setDelFor(a)}>
                          Delete
                        </button>
                      </div>
                    </figcaption>
                  </figure>
                );
              })}
            </div>

            {total > PAGE && (
              <div className="ann-filters" style={{ marginTop: 'var(--s4)' }}>
                <button className="btn-ghost btn-sm"
                        disabled={busy || offset === 0}
                        onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                  ← Newer
                </button>
                <span className="ann-tile-time">
                  {offset + 1}–{Math.min(offset + rows.length, total)} of {total}
                </span>
                <button className="btn-ghost btn-sm"
                        disabled={busy || offset + PAGE >= total}
                        onClick={() => setOffset(offset + PAGE)}>
                  Older →
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* The review posture: the full frame, the box on it, and the decision.
          ImageLightbox cannot be reused here — it has no way to draw the box,
          which is the only reason to open this. */}
      {zoomed && (
        <div className="overlay open ann-zoom"
             onMouseDown={e => { if (e.target === e.currentTarget) setZoom(null); }}>
          <div className="ann-zoom-inner">
            <Frame a={zoomed} color={colorOf[zoomed.class_key] ?? '#ff4444'}
                   aspect={(zoomed.frame.width || 16) / (zoomed.frame.height || 9)} />
            <div className="ann-zoom-bar">
              <button className="btn-ghost btn-sm" disabled={zoom === 0}
                      onClick={() => setZoom(z => Math.max(0, (z ?? 0) - 1))}>
                ← Prev
              </button>
              <span className="ann-zoom-meta">
                <b>{zoomed.class_key}</b> · {zoomed.camera_slug} ·{' '}
                {new Date(zoomed.captured_at).toLocaleString()} ·{' '}
                {Math.round(zoomed.bbox.width)}×{Math.round(zoomed.bbox.height)} px
                {' '}of {zoomed.frame.width}×{zoomed.frame.height}
                {' · '}{(zoom ?? 0) + 1} of {rows.length}
              </span>
              <button className="btn-ghost btn-sm"
                      disabled={zoom === rows.length - 1}
                      onClick={() => setZoom(z => Math.min(rows.length - 1,
                                                           (z ?? 0) + 1))}>
                Next →
              </button>
              <button className="btn-ghost btn-sm" onClick={() => setZoom(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* A reason is REQUIRED — see the header. window.prompt could not enforce
          that, and a blank one silently produced a kept still with no
          justification in the posture report. */}
      <Modal open={!!keepFor} title="Keep this annotation"
             onClose={() => setKeepFor(null)} width={520}
             footer={
               <div className="form-actions">
                 <button className="btn-primary" disabled={!keepWhy.trim() || busy}
                         data-action="keep-confirm" onClick={confirmKeep}>
                   Keep it
                 </button>
                 <button className="btn-ghost" onClick={() => setKeepFor(null)}>
                   Cancel
                 </button>
               </div>
             }>
        <p className="d-hint" style={{ marginTop: 0 }}>
          This clears the expiry, so the frame is held until somebody removes
          it. It is counted in the DPDP posture report as a still kept with no
          deadline, and this reason is what answers for it.
        </p>
        <div className="form-group">
          <label htmlFor="ann-keep-why">Why is it kept?</label>
          <textarea id="ann-keep-why" rows={3} value={keepWhy} maxLength={500}
                    autoFocus
                    placeholder="e.g. forklift training set — under-sampled class"
                    onChange={e => setKeepWhy(e.target.value)} />
          {!keepWhy.trim() && (
            <span className="d-hint" style={{ marginTop: 0 }}>
              Required.
            </span>
          )}
        </div>
      </Modal>

      <Modal open={!!delFor} title="Delete this annotation"
             onClose={() => setDelFor(null)} width={480}
             footer={
               <div className="form-actions">
                 <button className="btn-danger" disabled={busy}
                         data-action="delete-confirm" onClick={confirmDelete}>
                   Delete it
                 </button>
                 <button className="btn-ghost" onClick={() => setDelFor(null)}>
                   Cancel
                 </button>
               </div>
             }>
        <p style={{ fontSize: 13.5, lineHeight: 1.6, margin: 0 }}>
          The frame goes with it, and the footage it came from may have aged
          out — <b>this cannot be redrawn</b>. If you only picked the wrong
          class, change the class on the tile instead.
        </p>
      </Modal>
    </div>
  );
}
