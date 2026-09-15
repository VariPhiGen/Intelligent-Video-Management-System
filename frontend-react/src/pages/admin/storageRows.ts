/**
 * storageRows.ts — folding the NVR's per-recording-name storage into camera rows.
 *
 * The NVR reports storage per RECORDING NAME, and a camera with a sub track has
 * two of them. Left alone, `entry-g98a` and `entry-g98a_sub` arrive as separate
 * rows, sorted apart, labelled with the raw slug and showing no retention — to
 * an operator that reads as a mystery second camera. A sub is one camera's
 * low-resolution copy, not a camera, so it folds into its parent's total and is
 * disclosed as a portion of it ("incl. N low-res").
 *
 * Extracted from StorageTab because the folding is what makes the purge honest.
 * The row total is what the operator selects, is shown, and is told was freed —
 * so if `sub` is counted into `total` here, the purge has to delete the sub's
 * footage as well (the API's /nvr proxy fans a whole-camera purge out per
 * track). Those two halves used to disagree: the bytes were counted here and
 * only the main was deleted, so the freed figure was a claim about bytes that
 * were still on disk. Keeping this testable is how they stay in step.
 */
export const SUB_SUFFIX = '_sub';

export interface CameraStorage {
  /** Total bytes across every track of this camera, sub included. */
  total: number;
  /** Of that total, how much is the low-resolution sub track. */
  sub: number;
}

/** Bytes out of one per-camera entry, whichever key this NVR build used. */
export function bytesOf(v: any): number {
  return v?.total_bytes ?? v?.size_bytes ?? v?.bytes ?? 0;
}

/** Parent slug for a recording name — `x_sub` → `x`, anything else unchanged. */
export function parentSlug(name: string): string {
  return name.endsWith(SUB_SUFFIX) ? name.slice(0, -SUB_SUFFIX.length) : name;
}

/** Fold `per_camera` (keyed by recording name) into one entry per camera. */
export function foldPerCamera(
  perCamera: Record<string, any> | null | undefined,
): Map<string, CameraStorage> {
  const folded = new Map<string, CameraStorage>();
  for (const [name, v] of Object.entries(perCamera || {})) {
    const isSub = name.endsWith(SUB_SUFFIX);
    const parent = parentSlug(name);
    const acc = folded.get(parent) ?? { total: 0, sub: 0 };
    const b = bytesOf(v);
    acc.total += b;
    if (isSub) acc.sub += b;
    folded.set(parent, acc);
  }
  return folded;
}

/** Table rows, biggest first: [slug, total bytes]. */
export function storageRows(folded: Map<string, CameraStorage>): [string, number][] {
  return [...folded.entries()]
    .map(([name, v]): [string, number] => [name, v.total])
    .sort((a, b) => b[1] - a[1]);
}
