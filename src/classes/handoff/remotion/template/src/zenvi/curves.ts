// Keyframe evaluation with Remotion's own interpolate() + Easing.bezier, following Zenvi's
// (libopenshot's) rules: before the first key -> its value, after the last -> its value, on a key
// -> exactly its value, between two keys -> the easing stored on the second key.
import {Easing, interpolate} from 'remotion';
import type {CropSpec, FilterSpec, Keys} from './types';

const clamp01 = (x: number): number => Math.min(1, Math.max(0, x));

export const valueAt = (keys: Keys | undefined, frame: number, fallback = 0): number => {
  if (!keys || keys.length === 0) {
    return fallback;
  }
  const first = keys[0];
  if (keys.length === 1 || frame <= first.frame) {
    return first.value;
  }
  const last = keys[keys.length - 1];
  if (frame >= last.frame) {
    return last.value;
  }
  for (let i = 1; i < keys.length; i++) {
    const b = keys[i];
    if (frame > b.frame) {
      continue;
    }
    if (frame === b.frame) {
      return b.value;
    }
    const a = keys[i - 1];
    if (b.easing === 'hold' || a.frame === b.frame) {
      return b.easing === 'hold' ? a.value : b.value;
    }
    const easing = Array.isArray(b.easing)
      ? Easing.bezier(clamp01(b.easing[0]), b.easing[1], clamp01(b.easing[2]), b.easing[3])
      : Easing.linear;
    return interpolate(frame, [a.frame, b.frame], [a.value, b.value], {
      easing,
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
  }
  return last.value;
};

// CSS filter() for the effects Zenvi could map (brightness/contrast/saturate/hue/blur/invert).
export const filterAt = (filters: FilterSpec[], frame: number): string | undefined => {
  if (filters.length === 0) {
    return undefined;
  }
  return filters.map((f) => `${f.fn}(${valueAt(f.keys, frame, 0)}${f.unit})`).join(' ');
};

// Zenvi's Crop effect (fractions of the source cut from each side) as a CSS clip-path.
export const cropAt = (crop: CropSpec | null, frame: number): string | undefined => {
  if (!crop) {
    return undefined;
  }
  const pct = (keys: Keys): string => `${clamp01(valueAt(keys, frame, 0)) * 100}%`;
  return `inset(${pct(crop.top)} ${pct(crop.right)} ${pct(crop.bottom)} ${pct(crop.left)})`;
};
