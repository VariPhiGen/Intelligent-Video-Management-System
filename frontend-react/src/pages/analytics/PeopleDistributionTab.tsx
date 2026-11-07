/**
 * People Distribution — how many people are in each zone, now and through a day.
 *
 * ONE VIEW. A camera and a date (defaults: first camera alphabetically, today)
 * drive everything below them:
 *   - the live zone counts, shown only when the date is today;
 *   - "People in zone": peak and average occupancy per interval, as lines;
 *   - "Dwell time": average and longest stay per interval.
 * There is deliberately no "different people per hour" chart: it counted
 * distinct tracker ids, and a tracker hands the same person a new id whenever
 * it loses them for a moment, so the number read as far more people than came.
 * The live counts refresh every 15 s and today's charts every minute, so they keep
 * up with the day without refetching a whole day on every tick. A past day is
 * fixed and is fetched once.
 *
 * NEVER CLAIMS A COUNT IT CANNOT SUPPORT. A camera that is configured but has
 * not reported is shown as "waiting", not as zero, and an interval with no
 * samples is a gap in the chart, not a zero. The API decides staleness.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  INTERVALS, agoText, durationText, fetchDistributionBuckets,
  fetchDistributionLive, localDay,
  type BucketReading, type DistributionCamera, type LiveOverview,
} from '@/lib/peopleDistribution';
import { DistributionChart, type ChartPoint, type ChartSeries } from './DistributionChart';

const LIVE_REFRESH_MS = 15_000;
const CHART_REFRESH_MS = 60_000;

const OCCUPANCY: ChartSeries[] = [
  { key: 'peak', label: 'Most at once', color: 'var(--ee-entry)', kind: 'line' },
  { key: 'avg', label: 'Average', color: 'var(--ee-exit)', kind: 'line' },
];
const DWELL: ChartSeries[] = [
  { key: 'avg', label: 'Average stay', color: 'var(--ee-entry)', kind: 'bar' },
  { key: 'max', label: 'Longest stay', color: 'var(--ee-exit)', kind: 'mark' },
];

const zoneLabel = (z: string) => (z === '__unzoned__' ? 'Outside any zone' : z);

/** Local midnight of a "YYYY-MM-DD" day, epoch ms. */
function dayStart(day: string): number {
  const [y, m, d] = day.split('-').map(Number);
  return new Date(y, m - 1, d).getTime();
}

function ZoneTable({ cam }: { cam: DistributionCamera }) {
  const s = cam.sample;
  if (!s) {
    return (
      <p className="hm-note">
        Configured, but no occupancy has been reported yet for this camera.
      </p>
    );
  }
  return (
    <>
      <table className="pd-zones">
        <tbody>
          {s.zones.length === 0 && (
            <tr><td colSpan={2} className="hm-dim">No zones configured.</td></tr>
          )}
          {s.zones.map(z => (
            <tr key={z.zone_id}>
              <th scope="row">{zoneLabel(z.zone_name)}</th>
              <td>
                <strong>{z.current_count}</strong>
                <span className="hm-dim">
                  {' '}{z.current_count === 1 ? 'person' : 'people'}
                </span>
                {z.dwell_seconds_max > 0 && (
                  <span className="hm-dim pd-dwell">
                    {' '}· longest {Math.round(z.dwell_seconds_max)}s
                  </span>
                )}
              </td>
            </tr>
          ))}
          <tr className="pd-total">
            <th scope="row">Total</th>
            <td><strong>{s.total_count}</strong>
              <span className="hm-dim">
                {' '}{s.total_count === 1 ? 'person' : 'people'}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="hm-meta">
        <div>
          <span className="hm-k">Updated</span>
          <span className="hm-v">{agoText(s.age_seconds)}</span>
        </div>
        {!s.live && (
          <div className="hm-note">
            This is the last known count, not the current one — the camera has
            stopped reporting.
          </div>
        )}
      </div>
    </>
  );
}

function IntervalPicker({ value, onChange, label }: {
  value: number; onChange: (m: number) => void; label: string;
}) {
  return (
    <div className="ch-seg pd-seg" role="group" aria-label={label}>
      {INTERVALS.map(r => (
        <button key={r.minutes} className={r.minutes === value ? 'active' : ''}
                aria-pressed={r.minutes === value} onClick={() => onChange(r.minutes)}>
          {r.label}
        </button>
      ))}
    </div>
  );
}

function ChartCard({ title, note, picker, children }: {
  title: string; note: string; picker?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="panel pd-chart">
      <div className="pd-chart-head">
        <div>
          <h4 className="hm-h">{title}</h4>
          <p className="hm-dim pd-chart-note">{note}</p>
        </div>
        {picker}
      </div>
      {children}
    </section>
  );
}

/** Fetch `load()` now, again when `deps` change, and every `everyMs` while `live`. */
function usePolled<T>(load: () => Promise<T>, deps: unknown[], live: boolean, everyMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const d = await load();
        if (alive) { setData(d); setFailed(false); }
      } catch {
        if (alive) setFailed(true);
      }
    };
    setData(null);
    void pull();
    const timer = live ? setInterval(pull, everyMs) : undefined;
    return () => { alive = false; if (timer) clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, live, everyMs]);
  return { data, failed };
}

function toPoints(r: BucketReading | null, pick: (b: BucketReading['buckets'][number]) => Record<string, number | null>): ChartPoint[] {
  if (!r) return [];
  return r.buckets.map(b => {
    const startMs = Date.parse(b.start);
    return { startMs, endMs: startMs + r.minutes * 60_000,
             span: `${b.label}–${b.end_label}`, values: pick(b) };
  });
}

function CameraDay({ cam, day }: { cam: DistributionCamera; day: string }) {
  const today = day === localDay();
  const [occMin, setOccMin] = useState(60);
  const [dwellMin, setDwellMin] = useState(60);
  const start = dayStart(day);

  const occ = usePolled(() => fetchDistributionBuckets(cam.camera_id, day, occMin),
                        [cam.camera_id, day, occMin], today, CHART_REFRESH_MS);
  const dwell = usePolled(() => fetchDistributionBuckets(cam.camera_id, day, dwellMin),
                          [cam.camera_id, day, dwellMin], today, CHART_REFRESH_MS);

  const occPts = useMemo(() => toPoints(occ.data, b => ({ peak: b.people_peak, avg: b.people_avg })), [occ.data]);
  const dwellPts = useMemo(() => toPoints(dwell.data, b => ({ avg: b.dwell_avg_s, max: b.dwell_max_s })), [dwell.data]);

  const fmtCount = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));
  const failed = occ.failed || dwell.failed;

  return (
    <>
      {today && (
        <div className="panel hm-card pd-live">
          <div className="panel-head">
            <span className="live-dot" aria-hidden="true" /> <strong>Right now</strong>
            {!cam.camera_enabled && <span className="hm-dim"> · disabled</span>}
          </div>
          <div className="hm-meta"><ZoneTable cam={cam} /></div>
        </div>
      )}

      {failed && <div className="panel hm-warn">Some of this day could not be read.</div>}

      <ChartCard
        title="People in zone"
        note="Most people in the zones at the same moment, and the average, in each interval."
        picker={<IntervalPicker value={occMin} onChange={setOccMin} label="People in zone interval" />}>
        {occ.data ? (
          <DistributionChart dayStartMs={start} points={occPts} series={OCCUPANCY} scale="count"
                             format={fmtCount} label={`People in zone on ${day}`} />
        ) : <div className="skel pd-skel" />}
      </ChartCard>

      <ChartCard
        title="Dwell time"
        note="How long people stayed in the zones: bars are the average stay, the line on each bar is the longest single stay in that interval."
        picker={<IntervalPicker value={dwellMin} onChange={setDwellMin} label="Dwell time interval" />}>
        {dwell.data ? (
          <DistributionChart dayStartMs={start} points={dwellPts} series={DWELL} scale="duration"
                             format={durationText} label={`Dwell time on ${day}`} />
        ) : <div className="skel pd-skel" />}
      </ChartCard>

      {today && (
        <p className="hm-dim pd-foot">Charts refresh every minute; the counts above every 15 seconds.</p>
      )}
    </>
  );
}

export function PeopleDistributionTab({ onOpenConfig }: {
  /** Same hand-off as the Heatmap tab's empty state: the one thing to do next. */
  onOpenConfig?: () => void;
} = {}) {
  const [data, setData] = useState<LiveOverview | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [day, setDay] = useState(localDay());
  const [pick, setPick] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const d = await fetchDistributionLive();
        if (alive) { setData(d); setError(null); }
      } catch (e) {
        if (alive) setError(e as Error);
      }
    };
    void pull();
    const timer = setInterval(pull, LIVE_REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  const cameras = useMemo(
    () => [...(data?.cameras ?? [])].sort((a, b) =>
      (a.name || a.slug).localeCompare(b.name || b.slug, undefined, { sensitivity: 'base' })),
    [data]);
  const cam = cameras.find(c => c.camera_id === pick) ?? cameras[0];

  return (
    <div className="hm-page">
      {error && (
        <div className="panel hm-warn">
          People Distribution could not be read: {error.message}
        </div>
      )}
      {!data && !error && <div className="skel hm-skel-page" />}

      {data && !cameras.length && (
        <div className="emptystate">
          <span className="glyph" aria-hidden="true">◉</span>
          <h4>No camera counts people yet</h4>
          <p>Add <strong>People distribution</strong> to a camera in AI Config, and its
             zone occupancy will appear here as it is counted.</p>
          {onOpenConfig && (
            <button className="btn-primary" onClick={onOpenConfig}>Open AI Config</button>
          )}
        </div>
      )}

      {cam && (
        <>
          <div className="hm-controls">
            <label>
              <span className="hm-k">Camera</span>
              <select aria-label="Camera" value={cam.camera_id}
                      onChange={e => setPick(e.target.value)}>
                {cameras.map(c => (
                  <option key={c.camera_id} value={c.camera_id}>{c.name}</option>
                ))}
              </select>
            </label>
            <label>
              <span className="hm-k">Date</span>
              <input type="date" aria-label="Date" value={day} max={localDay()}
                     onChange={e => setDay(e.target.value || localDay())} />
            </label>
            {day !== localDay() && (
              <button className="btn-ghost" onClick={() => setDay(localDay())}>Back to today</button>
            )}
          </div>
          <CameraDay key={`${cam.camera_id}|${day}`} cam={cam} day={day} />
        </>
      )}
    </div>
  );
}
