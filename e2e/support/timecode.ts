/**
 * timecode.ts — reading the synthetic camera's burned-in clock out of recorded
 * video, so playback assertions are equalities rather than opinions.
 *
 * THIS IS THE WHOLE REASON THE CAMERA DRAWS A TIMECODE. Every other way of
 * checking that a scrub landed where it was asked is a judgement call: a
 * screenshot somebody has to look at, or a duration that drifts. A frame
 * carrying its own wall-clock second as pixels turns "did playback land on the
 * right moment" into `expect(decoded).toContain(requested)`.
 *
 * DECODING HAPPENS IN THE FIXTURE IMAGE, not here and not on the host. The
 * decoder needs the same ffmpeg that drew the text and the same font file; both
 * already live in vms-e2e-rtsp-cam, and keeping a second copy anywhere else is
 * exactly the drift that would make the decoder wrong in a way no test could
 * see. See e2e/fixtures/rtsp-cam/timecode.py for why it is glyph matching and
 * not OCR — tesseract misread a clean render badly enough to be unusable.
 */
import { execFile } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);

export interface DecodedTimecodes {
  /** How many frames were sampled. */
  frames: number;
  /** One epoch-second per frame, in order. `null` where a frame carried no
   *  readable timecode — which is itself a finding, not something to hide. */
  epochs: (number | null)[];
}

/**
 * Decode the timecode from every sampled frame of an MP4.
 *
 * `fps` is the sampling rate, not the video's: at 1 there is one reading per
 * second of footage, which matches the resolution the timecode is drawn at.
 * Asking for more just re-reads the same second.
 */
export async function decodeTimecodes(
  clip: Buffer,
  fps = 1,
): Promise<DecodedTimecodes> {
  const dir = mkdtempSync(join(tmpdir(), 'vms-e2e-tc-'));
  const file = join(dir, 'clip.mp4');
  try {
    writeFileSync(file, clip);
    const { stdout } = await run('docker', [
      'run', '--rm',
      '-v', `${dir}:/w`,
      '--entrypoint', 'python3',
      'vms-e2e-rtsp-cam',
      '/timecode.py', '/w/clip.mp4', String(fps),
    ], { timeout: 120_000, maxBuffer: 4 * 1024 * 1024 });
    return JSON.parse(stdout.trim()) as DecodedTimecodes;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/** The readable epochs only, dropping frames that carried none. */
export function readableEpochs(decoded: DecodedTimecodes): number[] {
  return decoded.epochs.filter((e): e is number => typeof e === 'number');
}

/**
 * A description of what a clip actually showed, for a failure message.
 *
 * A bare "expected 1789055315 to be in [...]" is much harder to act on than
 * "the clip covered 13:48:33-13:48:36 and you asked for 13:49:01".
 */
export function describeWindow(epochs: number[]): string {
  if (!epochs.length) return 'no readable timecodes';
  const iso = (e: number) => new Date(e * 1000).toISOString().replace('.000Z', 'Z');
  return `${iso(Math.min(...epochs))} … ${iso(Math.max(...epochs))} (${epochs.length} frames)`;
}
