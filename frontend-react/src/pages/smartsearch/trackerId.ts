/**
 * trackerId.ts — how a search result shows its tracker id, and where its box
 * goes on the frame.
 *
 * DISPLAY ONLY. The database keeps the complete id; this is what an operator
 * reads on a card, and where the overlay lands on the picture.
 */

/** The tracker id without the camera the card already names.
 *
 *  ObjectTracker._new_id builds `camera:nonce.epoch:counter`, so on a card that
 *  already prints the camera on the line above, the prefix is the same string
 *  twice — and it was the half being cut off, which left the useful half
 *  invisible. `cam2-6lpf:194379.0:18` -> `194379.0:18`.
 *
 *  MATCHED AGAINST THIS ROW'S CAMERA, not split on the first colon. The two
 *  agree on every row in this index (30,456 checked, zero mismatches), but a
 *  delimiter split would silently eat the first segment of any id that did not
 *  follow the pattern — a row from an older producer, say — and present the
 *  remainder as though it were complete. Anything that does not carry this
 *  camera's prefix is shown WHOLE instead, because a tracker id that is not
 *  shaped as expected is exactly the one worth seeing in full.
 *
 *  DISPLAY ONLY. The value copied, the value in the tooltip and the value in
 *  the database are all the complete id.
 */
export function trackerSuffix(trackerId: string, cameraId?: string): string {
  const prefix = cameraId ? `${cameraId}:` : '';
  return prefix && trackerId.startsWith(prefix)
    ? trackerId.slice(prefix.length)
    : trackerId;
}

/** Where to draw a detection's box over its frame, as CSS percentages.
 *
 *  The row's bbox is normalised [x1, y1, x2, y2] of the frame the object was
 *  found in, and the frame is stored downscaled WITH ITS ASPECT KEPT, so the
 *  same fractions hold on the picture at any size. Null for anything that is
 *  not a usable box — none on the row, or a malformed one — so the caller
 *  draws nothing rather than a box in the wrong place.
 */
export function boxStyle(bbox?: number[] | null):
  { left: string; top: string; width: string; height: string } | null {
  if (!bbox || bbox.length !== 4 || !bbox.every(Number.isFinite)) return null;
  const [x1, y1, x2, y2] = bbox.map(v => Math.min(1, Math.max(0, v)));
  if (x2 <= x1 || y2 <= y1) return null;
  const pct = (v: number) => `${+(v * 100).toFixed(3)}%`;
  return { left: pct(x1), top: pct(y1), width: pct(x2 - x1), height: pct(y2 - y1) };
}
