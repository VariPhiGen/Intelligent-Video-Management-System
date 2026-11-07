/**
 * wallLayout.ts — pick the video-wall grid that makes N camera tiles as large as possible on a
 * W×H stage. Tiles keep the cameras' 16:9 shape, so for every column count we size the tile to
 * fit both the width and the height and keep the arrangement with the largest tile (ties go to the
 * one that leaves fewer empty slots). 1 camera fills the stage; 18 on a wide screen lands on 6×3.
 */
export interface WallGrid { cols: number; rows: number; tileW: number; tileH: number }

/** Move one camera to another's position, retaining hidden cameras in the order. */
export function moveWallCamera(ids: string[], source: string, target: string): string[] {
  if (source === target || !ids.includes(source) || !ids.includes(target)) return ids;
  const next = ids.filter(id => id !== source);
  next.splice(ids.indexOf(target), 0, source);
  return next;
}

export function bestWallGrid(n: number, width: number, height: number, gap = 6, aspect = 16 / 9): WallGrid {
  const count = Math.max(1, Math.floor(n));
  const W = Math.max(1, width);
  const H = Math.max(1, height);
  let best: WallGrid & { area: number; empty: number } | null = null;
  for (let cols = 1; cols <= count; cols++) {
    const rows = Math.ceil(count / cols);
    let tileW = (W - gap * (cols - 1)) / cols;
    let tileH = tileW / aspect;
    if (tileH * rows + gap * (rows - 1) > H) {
      tileH = (H - gap * (rows - 1)) / rows;
      tileW = tileH * aspect;
    }
    if (tileW <= 0 || tileH <= 0) continue;
    const area = tileW * tileH;
    const empty = cols * rows - count;
    if (!best || area > best.area + 0.5 || (Math.abs(area - best.area) <= 0.5 && empty < best.empty)) {
      best = { cols, rows, tileW, tileH, area, empty };
    }
  }
  const b = best || { cols: 1, rows: count, tileW: W, tileH: W / aspect };
  return { cols: b.cols, rows: b.rows, tileW: Math.floor(b.tileW), tileH: Math.floor(b.tileH) };
}
