/**
 * homographyFit.ts — fitting the frame -> plan transform from clicked landmarks.
 *
 * This is the numeric half of camera registration, and it lives in the browser
 * on purpose. The backend deliberately carries no numeric dependency (see
 * services/homography.py), and the operator gets the reprojection error
 * updating as they place the fourth pin rather than after a round trip. The
 * backend still re-measures everything sent to it and rejects a degenerate
 * transform however well it fits — fitting here buys responsiveness, not trust.
 *
 * **The maths, and why it is written out rather than pulled from a library.**
 * A homography has 8 degrees of freedom (the 9th is scale, since H and 2H are
 * the same transform). Four point pairs give exactly 8 equations, so the system
 * is solvable directly:
 *
 *   x·h11 + y·h12 + h13 - u·x·h31 - u·y·h32 = u
 *   x·h21 + y·h22 + h23 - v·x·h31 - v·y·h32 = v
 *
 * with h33 pinned to 1 to fix the scale. Stacking those over n pairs gives an
 * over-determined 2n x 8 system, solved here in the least-squares sense via its
 * normal equations and Gaussian elimination with partial pivoting. No SVD, no
 * dependency, ~100 lines.
 *
 * **Pinning h33 = 1 has one failure case** and it is worth naming rather than
 * discovering: it cannot represent a transform whose true h33 is zero, which is
 * the case where the frame's origin corner maps to infinity — i.e. the top-left
 * pixel sits exactly on the horizon. Real registrations do not land there (the
 * operator would be pinning landmarks on the skyline), and if one ever did, the
 * solve becomes ill-conditioned and the reprojection error reports it rather
 * than the fit silently lying.
 *
 * **Hartley normalization is not optional.** Fitting directly on raw 0-1
 * coordinates produces a badly conditioned normal-equation matrix, because the
 * quadratic terms (u·x) are ~100x smaller than the linear ones. Translating
 * each point set to its centroid and scaling to a mean distance of sqrt(2)
 * fixes the conditioning, and the transform is undone afterwards so the caller
 * still gets a matrix in plain normalized coordinates. Skipping it gives a fit
 * that looks fine on well-spread landmarks and degrades sharply on clustered
 * ones — the exact case an operator is most likely to produce.
 */

export type Matrix3 = number[][];

/** One landmark: a point in the camera frame matched to a point on the plan.
 *  All four values normalized 0-1, matching every spatial value in this app. */
export interface Pair { fx: number; fy: number; mx: number; my: number }

/** A homography has 8 degrees of freedom, so four pairs is the minimum that
 *  DETERMINES one — but five is the minimum that lets anyone check it, and that
 *  is the number enforced here and in backend/models.py.
 *
 *  The reason is worth stating plainly, because the failure is silent and
 *  measured: at exactly four pairs the solve is exact, so the fitted transform
 *  passes through every landmark and the reprojection error is zero **whatever
 *  the operator clicked**. Moving one pin 20% of the plan away still reports
 *  0.000000000000. The number that is supposed to prove the registration is
 *  correct carries no information at all until a fifth pin over-determines the
 *  system. The fifth pin is not extra precision; it is the entire check. */
export const MIN_PAIRS = 5;

/** Below this many pairs there is not enough redundancy to attribute error to a
 *  single pin — see `suspectPin`. Two spare equations (six pairs) is where
 *  leave-one-out starts to separate a genuine outlier from ordinary noise. */
export const MIN_PAIRS_FOR_SUSPECT = 6;

/** Matches REGISTRATION_MAX_RMS in backend/models.py. Duplicated rather than
 *  fetched so the UI can disable Save before a round trip; the backend remains
 *  the enforcer, and a drift between the two shows up as a 422 on save rather
 *  than as a silently accepted bad registration. */
export const MAX_RMS = 0.02;

interface Norm { scale: number; cx: number; cy: number }

/** Hartley normalization for one point set: centre on the centroid, scale to a
 *  mean distance of sqrt(2) from it. Returns the transformed points and the
 *  parameters needed to undo it. */
function normalize(points: [number, number][]): { out: [number, number][]; n: Norm } {
  const count = points.length;
  const cx = points.reduce((s, p) => s + p[0], 0) / count;
  const cy = points.reduce((s, p) => s + p[1], 0) / count;
  const meanDist =
    points.reduce((s, p) => s + Math.hypot(p[0] - cx, p[1] - cy), 0) / count;
  // Coincident points have no spread to normalize against; scale 1 leaves them
  // alone and lets the solve below fail honestly on a singular system instead
  // of dividing by zero here.
  const scale = meanDist > 1e-12 ? Math.SQRT2 / meanDist : 1;
  return {
    out: points.map(([x, y]) => [(x - cx) * scale, (y - cy) * scale] as [number, number]),
    n: { scale, cx, cy },
  };
}

/** Solve `a · x = b` for a square `a`, by Gaussian elimination with partial
 *  pivoting. Returns null when the matrix is singular to working precision —
 *  the caller surfaces that as "landmarks are too close to a straight line"
 *  rather than propagating NaNs into a matrix that would still render. */
function solve(a: number[][], b: number[]): number[] | null {
  const n = b.length;
  // Work on a copy: the caller's normal-equation matrix is reused when the
  // operator adds another pin and refits.
  const m = a.map((row, i) => [...row, b[i]]);

  for (let col = 0; col < n; col++) {
    // Partial pivoting: without it, a zero (or tiny) pivot on a perfectly
    // solvable system destroys the result.
    let pivot = col;
    for (let r = col + 1; r < n; r++) {
      if (Math.abs(m[r][col]) > Math.abs(m[pivot][col])) pivot = r;
    }
    if (Math.abs(m[pivot][col]) < 1e-14) return null;
    [m[col], m[pivot]] = [m[pivot], m[col]];

    for (let r = col + 1; r < n; r++) {
      const factor = m[r][col] / m[col][col];
      if (factor === 0) continue;
      for (let c = col; c <= n; c++) m[r][c] -= factor * m[col][c];
    }
  }

  const x = new Array<number>(n).fill(0);
  for (let row = n - 1; row >= 0; row--) {
    let sum = m[row][n];
    for (let c = row + 1; c < n; c++) sum -= m[row][c] * x[c];
    x[row] = sum / m[row][row];
  }
  return x.every(Number.isFinite) ? x : null;
}

function multiply(a: Matrix3, b: Matrix3): Matrix3 {
  const out: Matrix3 = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < 3; i++) {
    for (let j = 0; j < 3; j++) {
      out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
    }
  }
  return out;
}

/** Project one point. Returns null at the horizon, where there is no finite
 *  answer — callers treat that as "this landmark cannot be described by this
 *  view" rather than substituting a number. */
export function apply(h: Matrix3, x: number, y: number): [number, number] | null {
  const w = h[2][0] * x + h[2][1] * y + h[2][2];
  if (Math.abs(w) < 1e-9) return null;
  return [
    (h[0][0] * x + h[0][1] * y + h[0][2]) / w,
    (h[1][0] * x + h[1][1] * y + h[1][2]) / w,
  ];
}

/** RMS distance, in plan units, between each landmark's clicked plan position
 *  and where `h` actually sends its frame position. This is the number the
 *  operator watches while placing pins; the backend recomputes it on save. */
export function reprojectionRms(h: Matrix3, pairs: Pair[]): number {
  if (!pairs.length) return Infinity;
  let total = 0;
  for (const p of pairs) {
    const projected = apply(h, p.fx, p.fy);
    if (!projected) return Infinity;
    total += (projected[0] - p.mx) ** 2 + (projected[1] - p.my) ** 2;
  }
  return Math.sqrt(total / pairs.length);
}

/** Per-landmark residual: how far this pin sits from where the fit sends it.
 *
 *  Useful for shading pins, and NOT a way to find a misplaced one. Least
 *  squares distributes an outlier's error across every residual, so the largest
 *  residual is frequently not the bad pin — measured on this implementation,
 *  a single 0.05 outlier among ten pairs ranked second. Use `suspectPin` for
 *  that question. */
export function perPairResidual(h: Matrix3, pairs: Pair[]): number[] {
  return pairs.map(p => {
    const projected = apply(h, p.fx, p.fy);
    return projected ? Math.hypot(projected[0] - p.mx, projected[1] - p.my) : Infinity;
  });
}

/**
 * Which pin, if any, is holding the fit back — by leave-one-out refitting.
 *
 * Refits without each pin in turn and reports the one whose removal improves
 * the remaining error most, provided the improvement is large enough to be a
 * misplaced pin rather than ordinary click noise. This is the honest version of
 * "which pin is wrong": it asks the question directly instead of reading it off
 * residuals that least squares has already smeared.
 *
 * Costs n refits of an 8x8 solve — microseconds, and it only runs when the
 * operator has stopped clicking.
 *
 * Returns null when there is nothing to report: too few pairs to spare one, no
 * candidate that helps, or an improvement too small to distinguish from noise.
 * Null means "no suggestion", never "all pins are good".
 */
export function suspectPin(pairs: Pair[], improvementFactor = 3): number | null {
  if (pairs.length < MIN_PAIRS_FOR_SUSPECT) return null;
  const full = fitHomography(pairs);
  if (!full) return null;
  const baseline = reprojectionRms(full, pairs);
  if (!Number.isFinite(baseline) || baseline === 0) return null;

  let best: { index: number; rms: number } | null = null;
  for (let i = 0; i < pairs.length; i++) {
    const without = pairs.filter((_, j) => j !== i);
    const fitted = fitHomography(without);
    if (!fitted) continue;
    const rms = reprojectionRms(fitted, without);
    if (!Number.isFinite(rms)) continue;
    if (!best || rms < best.rms) best = { index: i, rms };
  }
  // Dropping any point always helps a little, since there is one less residual
  // to satisfy. Only a pin that improves things severalfold is worth naming —
  // otherwise the UI would accuse a different innocent pin on every click.
  if (!best || best.rms * improvementFactor > baseline) return null;
  return best.index;
}

/**
 * Fit the frame -> plan homography from landmark pairs.
 *
 * Returns null when the pairs cannot determine one: fewer than four, or a
 * configuration that leaves the system singular (all landmarks on a line, or
 * several clicked at the same spot). Null is a UI state, not an error — the
 * operator is mid-placement and the card says what is missing.
 */
export function fitHomography(pairs: Pair[]): Matrix3 | null {
  if (pairs.length < MIN_PAIRS) return null;

  const { out: src, n: sn } = normalize(pairs.map(p => [p.fx, p.fy] as [number, number]));
  const { out: dst, n: dn } = normalize(pairs.map(p => [p.mx, p.my] as [number, number]));

  // Normal equations for the 2n x 8 least-squares system. Built directly as
  // A^T·A and A^T·b so nothing larger than 8x8 is ever allocated.
  const ata = Array.from({ length: 8 }, () => new Array<number>(8).fill(0));
  const atb = new Array<number>(8).fill(0);

  for (let i = 0; i < pairs.length; i++) {
    const [x, y] = src[i];
    const [u, v] = dst[i];
    const rows = [
      [x, y, 1, 0, 0, 0, -u * x, -u * y],
      [0, 0, 0, x, y, 1, -v * x, -v * y],
    ];
    const targets = [u, v];
    for (let r = 0; r < 2; r++) {
      for (let a = 0; a < 8; a++) {
        atb[a] += rows[r][a] * targets[r];
        for (let b = 0; b < 8; b++) ata[a][b] += rows[r][a] * rows[r][b];
      }
    }
  }

  const h = solve(ata, atb);
  if (!h) return null;

  const normalized: Matrix3 = [
    [h[0], h[1], h[2]],
    [h[3], h[4], h[5]],
    [h[6], h[7], 1],
  ];

  // Undo both normalizations: H = Tdst^-1 · Hn · Tsrc, where T translates to the
  // centroid then scales.
  const tSrc: Matrix3 = [
    [sn.scale, 0, -sn.scale * sn.cx],
    [0, sn.scale, -sn.scale * sn.cy],
    [0, 0, 1],
  ];
  const tDstInv: Matrix3 = [
    [1 / dn.scale, 0, dn.cx],
    [0, 1 / dn.scale, dn.cy],
    [0, 0, 1],
  ];
  const result = multiply(tDstInv, multiply(normalized, tSrc));

  if (result.some(row => row.some(value => !Number.isFinite(value)))) return null;

  // Rescale so h33 is 1 where possible. Presentational only (a homography is
  // defined up to scale), but it keeps the stored numbers in a readable range
  // and makes two fits of the same view comparable by eye in a diff.
  const last = result[2][2];
  if (Math.abs(last) > 1e-12) {
    return result.map(row => row.map(value => value / last));
  }
  return result;
}


/**
 * The plan area a fitted view covers — for live preview while placing pins.
 *
 * Samples the frame border and drops points at or past the horizon, mirroring
 * services/homography.py so the preview and the stored footprint agree in
 * shape. It does NOT clip to the plan: the caller renders inside a viewBox that
 * clips visually, and an unclipped polygon is more informative while placing
 * pins because it shows the view running off the plan rather than stopping
 * neatly at the edge.
 *
 * Indicative only. The stored footprint is whatever the server computed on
 * save; if the two ever disagree visibly, that is a bug worth seeing.
 */
export function previewFootprint(h: Matrix3, samplesPerEdge = 16): [number, number][] {
  const border: [number, number][] = [];
  const step = 1 / samplesPerEdge;
  for (let i = 0; i < samplesPerEdge; i++) border.push([i * step, 0]);
  for (let i = 0; i < samplesPerEdge; i++) border.push([1, i * step]);
  for (let i = 0; i < samplesPerEdge; i++) border.push([1 - i * step, 1]);
  for (let i = 0; i < samplesPerEdge; i++) border.push([0, 1 - i * step]);

  const out: [number, number][] = [];
  for (const [x, y] of border) {
    const projected = apply(h, x, y);
    if (projected) out.push(projected);
  }
  return out.length >= 3 ? out : [];
}
