/** How a tracker id is shown on a search result, and where its box is drawn.
 *
 *  DISPLAY ONLY. The database keeps the complete id and so does the clipboard;
 *  what these pin is the string an operator READS on a card, which was
 *  previously `cam2-6lpf:1943…` — the camera repeated from the line above,
 *  followed by an ellipsis where the distinguishing half used to be.
 *
 *  The shape comes from ObjectTracker._new_id: `camera:nonce.epoch:counter`.
 *  Verified against the live index on 2026-09-10 — 30,456 rows across
 *  search_persons and search_vehicles, every one prefixed with its own camera,
 *  no camera slug containing a colon, and at most 15 characters left after the
 *  prefix is removed.
 */
import { describe, it, expect } from 'vitest';
import { boxStyle, trackerSuffix } from './trackerId';

describe('trackerSuffix', () => {
  it('drops the camera the card already names', () => {
    expect(trackerSuffix('cam2-6lpf:194379.0:18', 'cam2-6lpf')).toBe('194379.0:18');
  });

  it('keeps the nonce, the epoch and the counter', () => {
    // All three are what make the id unambiguous across a restart or a
    // reconnect, so none of them may be trimmed for width.
    expect(trackerSuffix('cam1-h5e0:d06e41.0:429', 'cam1-h5e0')).toBe('d06e41.0:429');
  });

  it("leaves an id alone when the prefix is not this row's camera", () => {
    // A delimiter split would have eaten `othercam` and shown `194379.0:18`
    // as though it were complete. An id that is not shaped as expected is
    // exactly the one worth seeing in full.
    expect(trackerSuffix('othercam:194379.0:18', 'cam2-6lpf'))
      .toBe('othercam:194379.0:18');
  });

  it('leaves an id alone when the row has no camera', () => {
    expect(trackerSuffix('cam2-6lpf:194379.0:18', undefined))
      .toBe('cam2-6lpf:194379.0:18');
  });

  it('does not strip a camera that is only a partial match', () => {
    // `cam2` is a prefix of `cam2-6lpf` as a STRING but not as a segment.
    // Requiring the colon is what stops `-6lpf:194379.0:18` being shown.
    expect(trackerSuffix('cam2-6lpf:194379.0:18', 'cam2'))
      .toBe('cam2-6lpf:194379.0:18');
  });

  it('handles an id that is nothing but the prefix', () => {
    expect(trackerSuffix('cam2-6lpf:', 'cam2-6lpf')).toBe('');
  });

  it('is idempotent — a suffix passed back through is unchanged', () => {
    const once = trackerSuffix('cam2-6lpf:194379.0:18', 'cam2-6lpf');
    expect(trackerSuffix(once, 'cam2-6lpf')).toBe(once);
  });

  it('never returns more than the id it was given', () => {
    const full = 'cam5-iohm:d06e41.0:1543';
    expect(trackerSuffix(full, 'cam5-iohm').length).toBeLessThanOrEqual(full.length);
  });
});

describe('boxStyle', () => {
  it('turns a normalised bbox into percentages of the picture', () => {
    expect(boxStyle([0.25, 0.5, 0.75, 1])).toEqual(
      { left: '25%', top: '50%', width: '50%', height: '50%' });
  });

  it('keeps a box that overhangs the frame on the picture', () => {
    // Detector boxes can run a pixel past the edge; clamped, not dropped.
    expect(boxStyle([-0.01, 0, 0.5, 1.02])).toEqual(
      { left: '0%', top: '0%', width: '50%', height: '100%' });
  });

  it('draws nothing rather than a box in the wrong place', () => {
    expect(boxStyle(null)).toBeNull();
    expect(boxStyle(undefined)).toBeNull();
    expect(boxStyle([])).toBeNull();
    expect(boxStyle([0.1, 0.1, 0.2])).toBeNull();
    expect(boxStyle([0.5, 0.5, 0.2, 0.9])).toBeNull();      // inverted
    expect(boxStyle([0.1, NaN, 0.2, 0.4])).toBeNull();
  });
});
