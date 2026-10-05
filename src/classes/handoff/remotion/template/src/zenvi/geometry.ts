// Where Zenvi (libopenshot 1.0, Clip::get_transform) draws a clip's source image on the canvas.
// A line-by-line port of Zenvi's classes/handoff/transform.py `geometry()`; Zenvi's tests run
// this file with Node and compare the matrices. No imports, so it runs anywhere.
//
// The result is a CSS matrix(a, b, c, d, e, f) for an element sized to the SOURCE image
// (width x height = source pixels) with `transform-origin: 0 0`.

export const SCALE_CROP = 0;
export const SCALE_FIT = 1;
export const SCALE_STRETCH = 2;
export const SCALE_NONE = 3;

export type Pose = {
  scaleX: number;
  scaleY: number;
  locationX: number;
  locationY: number;
  rotation: number;
  originX: number;
  originY: number;
  shearX: number;
  shearY: number;
  margin: number;
};

export type Matrix = [number, number, number, number, number, number];

const near = (a: number, b: number): boolean => Math.abs(a - b) < 0.000001;

// QSize::scaled: integer math, truncating; 'ignore' | 'keep' | 'expand'.
export const qsizeScaled = (w: number, h: number, tw: number, th: number, mode: string): [number, number] => {
  if (mode === 'ignore' || w === 0 || h === 0) {
    return [tw, th];
  }
  const rw = Math.trunc((th * w) / h);
  const useHeight = mode === 'keep' ? rw <= tw : rw >= tw;
  return useHeight ? [rw, th] : [tw, Math.trunc((tw * h) / w)];
};

// SCALE_NONE: libopenshot decodes the source at its size times the clip's LARGEST scale first
// (stills scale to that box, video only shrinks, keeping its aspect), then draws it at `scale` again --
// a 1920x1080 video at scale 0.5 shows 480x270. Port of transform.py `delivered_size`.
export const deliveredSize = (
  srcW: number, srcH: number, maxScaleX: number, maxScaleY: number, still: boolean,
): [number, number] => {
  const boxW = Math.trunc(srcW * maxScaleX);
  const boxH = Math.trunc(srcH * maxScaleY);
  if (still) {
    return boxW > 0 && boxH > 0 ? qsizeScaled(srcW, srcH, boxW, boxH, 'keep') : [srcW, srcH];
  }
  if (boxW !== 0 && boxH !== 0 && boxW < srcW && boxH < srcH) {
    const ratio = srcW / srcH;
    const possibleW = Math.floor(boxH * ratio + 0.5);
    const possibleH = Math.floor(boxW / ratio + 0.5);
    return possibleW <= boxW ? [possibleW, boxH] : [boxW, possibleH];
  }
  return [srcW, srcH];
};

export const scaledSourceSize = (
  srcW: number, srcH: number, scaleMode: number, boxW: number, boxH: number,
): [number, number] => {
  if (scaleMode === SCALE_FIT) {
    return qsizeScaled(srcW, srcH, boxW, boxH, 'keep');
  }
  if (scaleMode === SCALE_STRETCH) {
    return qsizeScaled(srcW, srcH, boxW, boxH, 'ignore');
  }
  if (scaleMode === SCALE_CROP) {
    return qsizeScaled(srcW, srcH, boxW, boxH, 'expand');
  }
  return [srcW, srcH];
};

// QTransform subset (row vectors): every op applies BEFORE the existing ones.
class Affine {
  m: Matrix = [1, 0, 0, 1, 0, 0];

  private pre(a11: number, a12: number, a21: number, a22: number, adx: number, ady: number): void {
    const [m11, m12, m21, m22, dx, dy] = this.m;
    this.m = [
      a11 * m11 + a12 * m21, a11 * m12 + a12 * m22,
      a21 * m11 + a22 * m21, a21 * m12 + a22 * m22,
      adx * m11 + ady * m21 + dx, adx * m12 + ady * m22 + dy,
    ];
  }

  translate(dx: number, dy: number): void {
    this.pre(1, 0, 0, 1, dx, dy);
  }

  scale(sx: number, sy: number): void {
    this.pre(sx, 0, 0, sy, 0, 0);
  }

  rotate(degrees: number): void {
    const rad = (degrees * Math.PI) / 180;
    let c = Math.cos(rad);
    let s = Math.sin(rad);
    const mod = ((degrees % 360) + 360) % 360;
    if (mod === 0) {
      c = 1;
      s = 0;
    } else if (mod === 90) {
      c = 0;
      s = 1;
    } else if (mod === 180) {
      c = -1;
      s = 0;
    } else if (mod === 270) {
      c = 0;
      s = -1;
    }
    this.pre(c, s, -s, c, 0, 0);
  }

  shear(sh: number, sv: number): void {
    this.pre(1, sv, sh, 1, 0, 0);
  }
}

// `maxScale` (SCALE_NONE only): the largest scale_x / scale_y keyframe value of the whole clip
// (default: this instant's); `still`: the source is an image.
export const clipMatrix = (
  srcW: number, srcH: number, canvasW: number, canvasH: number, scaleMode: number, gravity: number, pose: Pose,
  maxScale: [number, number] | null = null, still = false,
): Matrix => {
  const sw = Math.max(1, Math.round(srcW || canvasW));
  const sh = Math.max(1, Math.round(srcH || canvasH));
  const width = canvasW;
  const height = canvasH;
  const margin = Math.max(0, Math.min(0.5, pose.margin));
  const marginPx = margin * Math.min(width, height);
  const layoutX = marginPx;
  const layoutY = marginPx;
  const layoutW = Math.max(1, width - marginPx * 2);
  const layoutH = Math.max(1, height - marginPx * 2);
  const [sizeW, sizeH] = scaleMode === SCALE_NONE
    ? deliveredSize(sw, sh, maxScale ? maxScale[0] : pose.scaleX, maxScale ? maxScale[1] : pose.scaleY, still)
    : scaledSourceSize(sw, sh, scaleMode, Math.trunc(layoutW), Math.trunc(layoutH));
  const ssw = sizeW * pose.scaleX;
  const ssh = sizeH * pose.scaleY;

  const cx = layoutX + (layoutW - ssw) / 2;
  const cy = layoutY + (layoutH - ssh) / 2;
  const rx = layoutX + layoutW - ssw;
  const by = layoutY + (layoutH - ssh);
  const xs = [layoutX, cx, rx, layoutX, cx, rx, layoutX, cx, rx];
  const ys = [layoutY, layoutY, layoutY, cy, cy, cy, by, by, by];
  let x = gravity >= 0 && gravity <= 8 ? xs[gravity] : 0;
  let y = gravity >= 0 && gravity <= 8 ? ys[gravity] : 0;

  const lx = pose.locationX;
  const ly = pose.locationY;
  if (scaleMode === SCALE_CROP) {
    const offset = (location: number, anchored: number, canvas: number, clipSize: number): number =>
      location < 0 ? location * (anchored + clipSize) : location * (canvas - anchored);
    x = x + offset(lx, x - layoutX, layoutW, ssw);
    y = y + offset(ly, y - layoutY, layoutH, ssh);
  } else {
    x = width * lx + x;
    y = height * ly + y;
  }

  const t = new Affine();
  if (!near(x, 0) || !near(y, 0)) {
    t.translate(x, y);
  }
  const oxo = ssw * pose.originX;
  const oyo = ssh * pose.originY;
  if (!near(pose.rotation, 0) || !near(pose.shearX, 0) || !near(pose.shearY, 0)) {
    t.translate(oxo, oyo);
    t.rotate(pose.rotation);
    t.shear(pose.shearX, pose.shearY);
    t.translate(-oxo, -oyo);
  }
  const sws = (sizeW / sw) * pose.scaleX;
  const shs = (sizeH / sh) * pose.scaleY;
  if (!near(sws, 1) || !near(shs, 1)) {
    t.scale(sws, shs);
  }
  return t.m;
};

export const cssMatrix = (m: Matrix): string => `matrix(${m.map((v) => (Math.abs(v) < 1e-12 ? 0 : v)).join(', ')})`;
