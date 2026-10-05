// The shape of src/zenvi/timeline.json (written by Zenvi; "zenvi_timeline": 1).
//
// Frames are composition frames (0-based). A clip's keyframe frames count from the clip's
// first visible frame. Each keyframe's `easing` shapes the segment that ENDS at it:
// 'linear', 'hold' (keep the previous value until this keyframe) or a cubic-bezier
// [x1, y1, x2, y2] -- Zenvi's (libopenshot's) convention.

export type KeyEasing = 'linear' | 'hold' | [number, number, number, number] | null;

export type Key = {
  frame: number;
  value: number;
  easing: KeyEasing;
};

export type Keys = Key[];

export type ClipKind = 'video' | 'image' | 'title' | 'audio';

export type TimeSpec =
  | {mode: 'normal' | 'rate'; trimBefore: number; playbackRate: number}
  | {mode: 'freeze'; trimBefore: number}
  | {mode: 'map'; map: number[]};

export type FilterSpec = {
  fn: 'brightness' | 'contrast' | 'saturate' | 'hue-rotate' | 'blur' | 'invert';
  unit: '' | 'deg' | 'px';
  keys: Keys;
};

export type CropSpec = {left: Keys; right: Keys; top: Keys; bottom: Keys};

export type ZenviClipData = {
  id: string;
  title: string;
  fileId: string;
  track: number;
  layer: number;
  from: number;
  durationInFrames: number;
  kind: ClipKind;
  src: string;
  transparent: boolean;
  sourceWidth: number;
  sourceHeight: number;
  scaleMode: number;
  gravity: number;
  time: TimeSpec;
  keyframes: Record<string, Keys>;
  hasAudio: boolean;
  hasVideo: boolean;
  filters: FilterSpec[];
  crop: CropSpec | null;
  blendMode: string | null;
};

export type ZenviTransitionData = {
  id: string;
  title: string;
  track: number;
  layer: number;
  from: number;
  durationInFrames: number;
  kind: 'fade' | 'wipe';
  mask: string | null;
  opacity: number[];
};

export type ZenviMediaData = {
  src: string;
  name: string;
  type: ClipKind;
  width: number;
  height: number;
  duration: number;
  hasAudio: boolean;
  hasVideo: boolean;
};

export type ZenviTimelineData = {
  zenvi_timeline: number;
  source_project: string;
  generator: string;
  composition: {id: string; width: number; height: number; fps: number; durationInFrames: number};
  background: string;
  tracks: {index: number; layer: number; name: string; locked: boolean}[];
  media: Record<string, ZenviMediaData>;
  clips: ZenviClipData[];
  transitions: ZenviTransitionData[];
  markers: {frame: number; time: number; name: string; color: string}[];
};
