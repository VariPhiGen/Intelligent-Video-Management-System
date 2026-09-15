/**
 * searchUi.tsx — Smart Search's shared vocabulary and its two smallest
 * presentational pieces.
 *
 * Split out of SmartSearchPage so the score bands live next to the component
 * that draws them: the numbers below are properties of the model on this index,
 * not styling choices, and they were easy to miss inside a 700-line page.
 */
import type { DomainStats } from '@/lib/smartsearch';

export type Mode = 'people' | 'vehicles';

/** Example queries — the fastest way to teach what "natural language" means here. */
export const EXAMPLES: Record<Mode, string[]> = {
  people: ['man in a red shirt', 'person carrying a backpack', 'someone in hi-vis vest', 'woman with an umbrella'],
  vehicles: ['white delivery van', 'red motorcycle', 'silver sedan', 'truck at the gate'],
};

/** The band `ViT-SO400M-14-SigLIP-384` actually produces on this index. Cosine
 *  similarity between a text embedding and an image embedding is unitless and
 *  does NOT span 0..1: measured against the live `persons` collection on
 *  2026-08-20, an unrelated query topped out around 0.05 and the best real
 *  description reached 0.179. Nothing ever approaches 1.0. */
export const SCORE_FLOOR = 0.05;
export const SCORE_CEIL = 0.20;

/** Below this the top hit is not distinguishable from what a meaningless query
 *  returns. Measured tops on the same index: gibberish 0.121, a person's name
 *  0.151, "man in a red shirt" 0.179. A warning, not a cutoff. */
export const WEAK_TOP_SCORE = 0.155;

/** Indexed entries for this VMS's own cameras. Nothing is shown when the count
 *  is unknown — the index is shared, and a number that turned out to be another
 *  deployment's is worse than no number at all. */
export function tabCount(d: DomainStats | undefined) {
  if (!d || d.vectors_count == null) return null;
  return <span className="ss-tab-count">{d.vectors_count.toLocaleString()}</span>;
}

/** Score bar — the number is the reading; the bar only ranks. It used to be
 *  `value * 100`, which assumed a 0..1 scale and therefore drew every result as
 *  an identical 5–18% stub. Normalise against the band above instead. */
export function Score({ value }: { value: number }) {
  const norm = (value - SCORE_FLOOR) / (SCORE_CEIL - SCORE_FLOOR);
  const pct = Math.max(2, Math.min(100, Math.round(norm * 100)));
  return (
    <div className="ss-score" title={`Similarity ${value.toFixed(4)} — cosine distance, not a confidence. Bar is scaled to the ${SCORE_FLOOR}–${SCORE_CEIL} band this index produces.`}>
      <div className="ss-score-track"><div className="ss-score-fill" style={{ width: `${pct}%` }} /></div>
      <span className="ss-score-num">{value.toFixed(3)}</span>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="ss-field">
      <span className="ss-field-label">{label}</span>
      {children}
    </label>
  );
}
