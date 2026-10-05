// The shape of src/zenvi/timeline.json (written by Zenvi; "zenvi_timeline": 1).
//
// A clip's timing is `position` (timeline start), `start` (source in) and `end` (source out), in
// seconds: the renderer and Zenvi's round trip both read these. Its keyframe frames are clip frames:
// 0 is the clip's first frame before any trim, so trimming keeps keyframes on the source. Each
// keyframe's `easing` shapes the segment that ENDS at it: 'linear', 'hold' (keep the previous value
// until this keyframe) or a cubic-bezier [x1, y1, x2, y2] -- Zenvi's (libopenshot's) convention.
// Transitions and markers use composition frames (0-based).

export type KeyEasing = 'linear' | 'hold' | [number, number, number, number] | null;

export type Key = {
  frame: number;
  value: number;
  easing: KeyEasing;
};

export type Keys = Key[];

export type ClipKind = 'video' | 'image' | 'title' | 'audio';

// normal: the media plays from `start`. rate / map were sampled for `forTrim` trimmed clip frames;
// the renderer shifts them when `start` changes.
export type TimeSpec =
  | {mode: 'normal'}
  | {mode: 'rate'; trimBefore: number; playbackRate: number; forTrim: number}
  | {mode: 'freeze'; trimBefore: number}
  | {mode: 'map'; map: number[]; forTrim: number};

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
  position: number;
  start: number;
  end: number;
  kind: ClipKind;
  src: string;
  transparent: boolean;
  sourceWidth: number;
  sourceHeight: number;
  scaleMode: number;
  gravity: number;
  maxScale: [number, number] | null;
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
  markers: {id: string; frame: number; time: number; name: string; color: string}[];
};
