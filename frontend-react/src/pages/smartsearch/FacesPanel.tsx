/**
 * Faces — search by photograph, and a gallery of what the index has collected.
 *
 * IT IS A SEPARATE VIEW FOR THE SAME REASON ANPR IS. People and Vehicles ask
 * "what looked like this description?" and take text. A face takes a picture:
 * the vectors here are SFace, which has no text encoder at all, so there is
 * nothing for a query box to do. Putting it behind the People tab would have
 * offered a text field that silently cannot work — and the one text query
 * operators reach for first is a NAME, which measured *inside* the band of
 * scores that look like success on the person index.
 *
 * SEARCH, NOT IDENTIFICATION. No watchlist, no names, no enrolment. The answer
 * is a ranked set of stored faces with their scores visible, and the operator
 * judges. That boundary is the product's, not an accident of what was built.
 *
 * WHAT THE NUMBERS MEAN, measured on this product's own footage 2026-09-14 and
 * the reason the bands below are not borrowed from the other tabs:
 *
 *     the same person, two photographs   median cosine 0.387
 *     different people                   median cosine 0.117
 *     the SAME face, re-encoded at q60   0.875
 *
 * So a "good" face match is around 0.4, not 0.9 — a scale that would read as
 * failure anywhere else in this UI, and the reason the score is labelled rather
 * than dressed as a confidence.
 *
 * SEVERAL PHOTOS OF ONE PERSON, up to five. The index pools their faces into
 * one query, so the search is less hostage to one photo's pose and lighting.
 * It also reports two things this panel shows rather than swallows: which
 * photo had no usable face, and how closely the photos agree. Averaging two
 * DIFFERENT people gives a query that resembles neither and still ranks
 * plausibly, so agreement below WEAK — where a pair is indistinguishable from
 * unrelated faces — is said out loud.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  recentFaces, searchFacesByImage,
  type FaceHit, type FacePhotoReport, type SearchCamera,
} from '@/lib/smartsearch';
import { EventClipModal } from './EventClipModal';
// The SAME thumbnail the other tabs use. Crops are served by an AUTHENTICATED
// proxy, so a plain <img src="/api/search/image?..."> is a 401 and an empty
// tile — the browser cannot attach the bearer token to an image request. This
// fetches the bytes with the token and hands the element an object URL.
import { CropThumb } from './ResultCard';
import { Field } from './searchUi';

/** The bands SFace actually produces on this footage — see the header. A match
 *  below `WEAK` is not distinguishable from an unrelated face. */
const STRONG = 0.45;
const WEAK = 0.25;

/** Photos one search may combine. The index and the VMS proxy enforce the same
 *  limit; checked here as well so selecting six says so before any upload. */
const MAX_PHOTOS = 5;

function bandOf(score: number | null | undefined): { label: string; cls: string } {
  if (score == null) return { label: '', cls: '' };
  if (score >= STRONG) return { label: 'strong', cls: 'ss-face-strong' };
  if (score >= WEAK) return { label: 'possible', cls: 'ss-face-possible' };
  return { label: 'weak', cls: 'ss-face-weak' };
}

function when(ms: number): string {
  return new Date(ms).toLocaleString();
}

export function FacesPanel({ cameras }: { cameras: SearchCamera[] }) {
  const [hits, setHits] = useState<FaceHit[]>([]);
  const [searched, setSearched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [camSlug, setCamSlug] = useState('');
  const [minWidth, setMinWidth] = useState(0);
  const [previews, setPreviews] = useState<string[]>([]);
  const [reports, setReports] = useState<FacePhotoReport[]>([]);
  const [eventClip, setEventClip] = useState<{ cam: SearchCamera; whenMs: number } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  /** The gallery. Also what the tab falls back to after a search is cleared —
   *  an empty screen cannot distinguish "no faces here" from "not searched". */
  const loadRecent = useCallback(async () => {
    setBusy(true); setErr(null); setNote(null);
    try {
      const r = await recentFaces({
        limit: 60, camera: camSlug || null,
        min_width_px: minWidth || undefined,
      });
      setHits(r.results || []);
      setSearched(false);
    } catch (e: any) {
      setErr(e.message || 'Could not load faces');
      setHits([]);
    }
    setBusy(false);
  }, [camSlug, minWidth]);

  useEffect(() => { loadRecent(); }, [loadRecent]);

  // Each uploaded photo holds its bytes alive for as long as its object URL
  // exists. Release the previous set when a new search replaces it or "Clear
  // search" drops it, and the last set when the tab unmounts.
  useEffect(() => () => { previews.forEach(u => URL.revokeObjectURL(u)); }, [previews]);

  const runSearch = useCallback(async (files: File[]) => {
    setErr(null); setNote(null); setWarning(null);
    if (files.length > MAX_PHOTOS) {
      setErr(`Choose up to ${MAX_PHOTOS} photos of the same person — `
           + `${files.length} were selected.`);
      return;
    }
    const several = files.length > 1;
    setBusy(true);
    setReports([]);
    // Shown beside the results so it is obvious WHICH photos produced them.
    setPreviews(files.map(f => URL.createObjectURL(f)));
    try {
      const r = await searchFacesByImage(files, {
        top_k: 48, camera: camSlug || null,
        min_width_px: minWidth || undefined,
      });
      setHits(r.results || []);
      setSearched(true);
      setReports(r.photos || []);
      if (!r.faces_detected) {
        // THE ONE FAILURE THE OPERATOR CAN FIX. "No matches" would send them
        // looking for a person who may be standing in front of the camera;
        // the truth is that their photo had no findable face in it.
        setNote(r.detail || (several
          ? 'No face was found in any of those photos — try clearer, more front-on pictures.'
          : 'No face was found in that photo — try a clearer, more front-on picture.'));
      } else if ((r.results || []).length === 0) {
        setNote(several
          ? 'Faces were read from the photos, but nothing in the index is close to them.'
          : 'A face was read from the photo, but nothing in the index is close to it.');
      }
      if (r.agreement != null && r.agreement < WEAK) {
        setWarning(`These photos may not all show the same person: the least similar `
                 + `pair scores ${r.agreement.toFixed(3)}, in the range of unrelated faces. `
                 + `The search matched an average of them, which can resemble none of `
                 + `them — try each photo on its own.`);
      }
    } catch (e: any) {
      setErr(e.message || 'Face search failed');
      setHits([]);
    }
    setBusy(false);
  }, [camSlug, minWidth]);

  const used = reports.filter(p => p.used).length;
  const queryCaption = previews.length <= 1
    ? 'Searching for this face'
    : used > 0 && used < previews.length
      ? `Searching for one face across ${used} of ${previews.length} photos`
      : `Searching for one face across ${previews.length} photos`;

  return (
    <>
      {eventClip && (
        <EventClipModal
          cam={eventClip.cam}
          whenMs={eventClip.whenMs}
          onClose={() => setEventClip(null)}
        />
      )}

      <div className="panel">
        <div className="panel-body">
          <div className="ss-filters">
            <Field label="Photos">
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                multiple
                onChange={e => {
                  const files = Array.from(e.target.files ?? []);
                  if (files.length) runSearch(files);
                }}
              />
            </Field>
            <Field label="Camera">
              <select value={camSlug} onChange={e => setCamSlug(e.target.value)}>
                <option value="">All cameras</option>
                {cameras.map(c => (
                  <option key={c.slug} value={c.slug}>{c.name || c.slug}</option>
                ))}
              </select>
            </Field>
            <Field label="Minimum face size">
              {/* The quality floor, in the units the index stores. Measured on
                  this site: retrieval rank-1 was 24% below 40px against 40% at
                  64-111px, so this is the difference between a gallery worth
                  looking at and one that cannot answer. */}
              <select value={minWidth} onChange={e => setMinWidth(Number(e.target.value))}>
                <option value={0}>Any</option>
                <option value={64}>64px and wider</option>
                <option value={112}>112px and wider</option>
              </select>
            </Field>
            {searched && (
              <button
                className="btn"
                onClick={() => { setPreviews([]); setReports([]); setWarning(null); loadRecent(); }}
              >
                Clear search
              </button>
            )}
          </div>
          <div className="hint">
            Upload a photo of someone — or up to {MAX_PHOTOS} photos of the same
            person, ideally from different angles — to find other appearances of
            that face. This searches faces recorded by this VMS — it does not
            identify anyone, and there is no list of known people.
          </div>
        </div>
      </div>

      {err && <div className="panel row-err ss-error"><div className="panel-body">{err}</div></div>}
      {busy && <div className="ss-loading"><span className="spinner" /> Working…</div>}

      {note && !busy && (
        <div className="panel"><div className="panel-body hint">{note}</div></div>
      )}

      {warning && !busy && (
        <div className="panel">
          <div className="panel-body hint"><b>Check the photos.</b> {warning}</div>
        </div>
      )}

      {!busy && (
        <div className="ss-faces">
          {previews.length > 0 && searched && (
            <div className="ss-face-query">
              {previews.map((src, i) => {
                // A photo with no usable face is still shown, so the operator
                // sees WHICH one did not count, rather than a shorter row.
                const unused = reports[i] ? !reports[i].used : false;
                return (
                  <figure key={src} className={`ss-face-query-photo${unused ? ' is-unused' : ''}`}>
                    <img
                      src={src}
                      alt={previews.length === 1
                        ? 'The photo being searched for'
                        : `Search photo ${i + 1}`}
                    />
                    {unused && <figcaption>No face found — not used</figcaption>}
                  </figure>
                );
              })}
              <div className="ss-face-query-caption">{queryCaption}</div>
            </div>
          )}

          <div className="ss-face-grid">
            {hits.map(h => {
              const band = bandOf(h.score);
              return (
                <figure key={h.id} className={`ss-face-card ${band.cls}`}>
                  <CropThumb domain="face" id={h.id} className="ss-face-img" />
                  <figcaption>
                    <div className="ss-face-cam">{h.camera?.name || h.camera_id}</div>
                    <div className="ss-face-when">{when(h.when_ms)}</div>
                    {h.score != null && (
                      /* The number AND the word. On this scale 0.4 is a strong
                         match, which looks like a failure to anyone reading it
                         as a percentage. */
                      <div className="ss-face-score">
                        {h.score.toFixed(3)} <span className="ss-face-band">{band.label}</span>
                      </div>
                    )}
                    {h.face_width_px != null && (
                      <div className="ss-face-size">{h.face_width_px}px</div>
                    )}
                    {h.camera && (
                      <button
                        className="btn btn-sm"
                        onClick={() => setEventClip({ cam: h.camera!, whenMs: h.when_ms })}
                      >
                        View clip
                      </button>
                    )}
                  </figcaption>
                </figure>
              );
            })}
          </div>

          {hits.length === 0 && !err && !note && (
            <div className="panel">
              <div className="panel-body">
                <b>No faces recorded yet.</b>
                <div className="hint">
                  A camera contributes faces only when the <b>face</b> domain is
                  switched on for it in AI&nbsp;Config. Most cameras see faces
                  rarely — a face has to be roughly 40 pixels wide before it is
                  worth storing, which in practice means close range.
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}
