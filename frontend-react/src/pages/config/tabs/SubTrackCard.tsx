/**
 * SubTrackCard.tsx — find and switch on a camera's low-resolution second stream.
 *
 * Why an operator would: playback of an HEVC camera costs the server a
 * transcode per minute of footage watched, because no browser outside the Apple
 * family decodes HEVC. Most cameras already publish a smaller second stream, and
 * recording it gives playback something cheaper — often free, when that stream
 * happens to be H.264.
 *
 * Two steps on purpose. Finding one is a probe and costs nothing. Recording one
 * is a second stream on the appliance — a second ffmpeg and roughly 10-25% more
 * disk — so it is a separate, deliberate switch.
 */
import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera, SubTrack } from '@/lib/types';
import { useToast } from '@/components/Toast';

const SOURCE_LABEL: Record<string, string> = {
  onvif: 'from the camera’s ONVIF profiles',
  derived: 'derived from the main stream’s URL',
  manual: 'entered by hand',
};

/** What this stream will actually save, in the operator's terms.
 *
 * Judged from the sub alone. The server only ever stores a sub that is already
 * worth recording — either H.264 (nothing to convert) or materially smaller
 * (much less to convert) — so there is no "this will not help" case to render. */
function benefit(sub: SubTrack): string {
  return /^(h264|avc1)$/i.test(sub.codec)
    ? 'H.264 — playback can serve it as-is, with no conversion at all.'
    : 'Smaller picture — still converted for the browser, but far less of it.';
}

/** What it will cost, in gigabytes, because a percentage is not decidable.
 *
 * The obvious guess — a smaller picture is a smaller file — is wrong whenever
 * the main uses a more efficient codec. Measured on this fleet the true figure
 * ranged from +12% to +230% of a camera's storage, so the card states the
 * actual measured bitrate as GB/day rather than quoting an average that is
 * wrong by an order of magnitude on the cameras that matter. */
function cost(sub: SubTrack, keepDays: number | null): string {
  const mbps = sub.bitrate_mbps;
  if (mbps == null) {
    return 'Stores a second copy of everything. Its size was not measured — '
      + 'watch this camera’s storage after switching it on.';
  }
  const gbDay = (mbps * 86400) / 8 / 1000;
  const share = sub.main_bitrate_mbps
    ? ` — about ${Math.round((mbps / sub.main_bitrate_mbps) * 100)}% of what the main recording uses`
    : '';
  const total = keepDays
    ? ` Kept for ${keepDays} day${keepDays === 1 ? '' : 's'}, that is roughly `
      + `${Math.round(gbDay * keepDays)} GB.`
    : '';
  return `About ${gbDay.toFixed(1)} GB per day${share}.${total}`;
}

export function SubTrackCard({ camera, onSaved }: { camera: Camera; onSaved: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState<'probe' | 'toggle' | null>(null);
  const sub = camera.sub_track;
  // Blank = the appliance default. Kept as text so clearing the box means
  // "use the default" rather than "zero days".
  const [keep, setKeep] = useState(() => sub?.retention_days?.toString() ?? '');
  const keepDays = keep.trim() === '' ? null : Number(keep);

  const probe = async () => {
    setBusy('probe');
    try {
      const updated = await apiFetch<Camera>(
        `/cameras/${camera.id}/sub-track/resolve`, { method: 'POST' });
      toast(updated.sub_track
        ? `Found a ${updated.sub_track.codec.toUpperCase()} ${updated.sub_track.width}×${updated.sub_track.height} stream`
        : 'No second stream worth recording on this camera', updated.sub_track ? 'ok' : 'err');
      onSaved();
    } catch (e: any) {
      toast(e?.message || 'Could not check this camera', 'err');
    } finally { setBusy(null); }
  };

  const toggle = async (recording_enabled: boolean, retention_days = keepDays) => {
    setBusy('toggle');
    try {
      await apiFetch(`/cameras/${camera.id}/sub-track`,
                     { method: 'PUT', body: JSON.stringify({ recording_enabled, retention_days }) });
      toast(recording_enabled ? 'Recording the second stream' : 'Stopped recording the second stream', 'ok');
      onSaved();
    } catch (e: any) {
      toast(e?.message || 'Could not change this', 'err');
    } finally { setBusy(null); }
  };

  return (
    <div style={{ marginTop: 20 }}>
      <div className="card-title" style={{ margin: '0 0 8px' }}>Faster playback</div>

      {!sub && (
        <>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 10 }}>
            Most cameras publish a second, smaller stream alongside the main one.
            Recording it makes scrubbing through footage much faster, and on some
            cameras removes the server’s conversion work entirely. Checking takes
            a few seconds and changes nothing on its own.
          </div>
          <button className="btn" disabled={busy != null} onClick={probe}>
            {busy === 'probe' ? 'Checking the camera…' : 'Check for a second stream'}
          </button>
        </>
      )}

      {sub && (
        <>
          <div style={{
            display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 10px',
            fontSize: 12.5, padding: '10px 12px', border: '1px solid var(--border)',
            borderRadius: 8, marginBottom: 10,
          }}>
            <span style={{ color: 'var(--muted)' }}>Stream</span>
            <span><b>{sub.codec.toUpperCase()} {sub.width}×{sub.height}</b>
              {sub.fps ? <span style={{ color: 'var(--muted)' }}> · {sub.fps} fps</span> : null}</span>
            <span style={{ color: 'var(--muted)' }}>Found</span>
            <span style={{ color: 'var(--muted)' }}>{SOURCE_LABEL[sub.source] ?? sub.source}</span>
            <span style={{ color: 'var(--muted)' }}>Effect</span>
            <span>{benefit(sub)}</span>
            <span style={{ color: 'var(--muted)' }}>Storage</span>
            {/* Prefer the number the operator is typing, then what the server
                says will actually apply — a camera on the appliance default has
                no `retention_days` of its own but is still kept for a definite
                number of days, and the total is the useful half of the answer. */}
            <span>{cost(sub, sub.recording_enabled
              ? (keepDays ?? sub.effective_retention_days) : null)}</span>
          </div>

          <label style={{ display: 'flex', gap: 10, alignItems: 'flex-start', cursor: 'pointer' }}>
            <input
              type="checkbox" checked={sub.recording_enabled} disabled={busy != null}
              onChange={e => toggle(e.target.checked)} style={{ width: 'auto', marginTop: 3 }} />
            <span>
              <div style={{ fontSize: 13, fontWeight: 600 }}>Record this stream too</div>
              <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6 }}>
                It inherits this camera’s privacy masks, and is kept for its own
                shorter period (below) rather than the main’s — it is for
                scrubbing, not for evidence. Exports and evidence always use the
                full-quality main stream.
              </div>
            </span>
          </label>

          {sub.recording_enabled && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '10px 0 0 26px' }}>
              <span style={{ fontSize: 12.5 }}>Keep this stream for</span>
              <input
                type="number" min={1} max={3650} step={1} value={keep}
                onChange={e => setKeep(e.target.value)}
                onBlur={() => sub.recording_enabled && toggle(true)}
                placeholder={sub.effective_retention_days
                  ? `Default (${sub.effective_retention_days})` : 'Default'}
                style={{ width: 110 }}
              />
              <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>days</span>
            </div>
          )}
          {sub.recording_enabled && (
            <div style={{ fontSize: 11, color: 'var(--dim)', margin: '6px 0 0 26px', lineHeight: 1.6 }}>
              Shorter than the main recording on purpose — this stream is for
              scrubbing recent footage, not for evidence. Once it expires, older
              footage still plays from the full-quality main recording, just more
              slowly. Never longer than the main’s own retention.
            </div>
          )}

          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 8, lineHeight: 1.6 }}>
            {sub.recording_enabled
              ? 'Playback uses this stream for footage recorded from the moment it was switched on; anything older still plays from the main recording.'
              : 'Nothing is being recorded from this stream yet.'}
            {' '}
            <button className="btn-link" disabled={busy != null} onClick={probe}
                    style={{ padding: 0, font: 'inherit' }}>
              {busy === 'probe' ? 'Re-checking…' : 'Check again'}
            </button>
            {' '}if the camera’s stream settings have changed — the details above are
            from the last check, and a camera reconfigured since then will not match.
          </div>
        </>
      )}
    </div>
  );
}
