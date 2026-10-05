// Where Zenvi clips sit in time, how they stack and what sound they make -- plain functions (no
// React), shared by ZenviTimeline.tsx and ZenviClip.tsx.
import type {TimeSpec, ZenviClipData, ZenviTimelineData} from './types';

// Frames from seconds, rounded half up -- exactly as Zenvi's exporter does.
export const toFrame = (seconds: number, fps: number): number => Math.floor(seconds * fps + 0.5);

// Where a clip sits in the composition, from its position / start / end (seconds).
export const clipFrames = (clip: ZenviClipData, fps: number): {from: number; durationInFrames: number} => {
  const from = toFrame(clip.position, fps);
  const to = toFrame(clip.position + clip.end - clip.start, fps);
  return {from, durationInFrames: Math.max(1, to - from)};
};

// The composition's length: the end of the last clip or transition (edits that extend the timeline show up).
export const timelineDurationInFrames = (data: ZenviTimelineData): number => {
  const fps = data.composition.fps;
  let end = 1;
  for (const clip of data.clips) {
    const {from, durationInFrames} = clipFrames(clip, fps);
    end = Math.max(end, from + durationInFrames);
  }
  for (const t of data.transitions) {
    end = Math.max(end, t.from + t.durationInFrames);
  }
  return end;
};

// The order Zenvi (libopenshot) draws clips in: by track (layer number), then by position; later
// clips draw on top. Ties keep their order in timeline.json. A copy: the data is not changed.
export const drawOrder = (clips: ZenviClipData[]): ZenviClipData[] =>
  clips
    .map((clip, index) => ({clip, index}))
    .sort((a, b) => a.clip.layer - b.clip.layer || a.clip.position - b.clip.position || a.index - b.index)
    .map((entry) => entry.clip);

// How a clip's sound plays: the source frame it starts at and its rate -- or null when Remotion cannot
// play it (a held frame, reverse or a speed ramp: Zenvi's export notes say these have no sound).
// `trim` is the clip's trimmed frames (its `start` in frames).
export const audioPlayback = (time: TimeSpec, trim: number): {trimBefore: number; playbackRate: number} | null => {
  if (time.mode === 'normal') {
    return {trimBefore: trim, playbackRate: 1};
  }
  if (time.mode === 'rate') {
    return {trimBefore: time.trimBefore + Math.round((trim - time.forTrim) * time.playbackRate),
      playbackRate: time.playbackRate};
  }
  return null;
};
