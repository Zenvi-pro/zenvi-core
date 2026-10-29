// One Zenvi clip, drawn the way Zenvi (libopenshot 1.0) draws it: the source image placed by the
// clip's scale mode, gravity, location, scale, rotation, origin and shear (geometry.ts), its
// alpha as opacity, its volume curve as Remotion's volume callback, constant speed as
// playbackRate, holds / reverse / ramps frame by frame (without sound), and the mapped effects as
// CSS filters.
import React from 'react';
import {Audio, Freeze, Img, OffthreadVideo, staticFile, useCurrentFrame} from 'remotion';
import {cropAt, filterAt, valueAt} from './curves';
import {clipMatrix, cssMatrix} from './geometry';
import {audioPlayback} from './timing';
import type {ZenviClipData, ZenviTransitionData} from './types';

type Props = {
  clip: ZenviClipData;
  from: number; // the composition frame the clip's Sequence starts at
  fps: number;
  width: number;
  height: number;
  transitions: ZenviTransitionData[];
  volume: number;
};

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

// Zenvi transitions on this clip's track (fades exactly; other wipes as fades).
const transitionOpacity = (transitions: ZenviTransitionData[], timelineFrame: number): number => {
  let out = 1;
  for (const t of transitions) {
    const i = timelineFrame - t.from;
    if (i >= 0 && i < t.opacity.length) {
      out *= t.opacity[i];
    }
  }
  return out;
};

export const ZenviClip: React.FC<Props> = ({clip, from, fps, width, height, transitions, volume}) => {
  const frame = useCurrentFrame();
  const k = clip.keyframes;
  const src = staticFile(clip.src);
  // Keyframes are clip frames (0 = the clip's first frame before any trim): the clip shows them from its
  // trim on, so a changed `start` keeps them on the source, as in Zenvi.
  const trim = Math.floor(clip.start * fps + 0.5);
  const at = frame + trim;
  const volumeAt = (f: number): number => clamp(valueAt(k.volume, f + trim, 1), 0, 1) * volume;
  const time = clip.time;
  // the source frame shown at the clip's first visible frame, and its speed (normal / rate);
  // null for holds, reverse and ramps, which play their frames without sound
  const playback = audioPlayback(time, trim);
  const sourceStart = playback ? playback.trimBefore : 0;
  const rate = playback ? playback.playbackRate : 1;

  if (clip.kind === 'audio' || !clip.hasVideo) {
    if (!clip.hasAudio || playback === null) {
      return null;
    }
    return (
      <Audio src={src} trimBefore={sourceStart > 0 ? sourceStart : undefined} playbackRate={rate} volume={volumeAt} />
    );
  }

  const matrix = clipMatrix(clip.sourceWidth, clip.sourceHeight, width, height, clip.scaleMode, clip.gravity, {
    scaleX: valueAt(k.scale_x, at, 1),
    scaleY: valueAt(k.scale_y, at, 1),
    locationX: valueAt(k.location_x, at, 0),
    locationY: valueAt(k.location_y, at, 0),
    rotation: valueAt(k.rotation, at, 0),
    originX: valueAt(k.origin_x, at, 0.5),
    originY: valueAt(k.origin_y, at, 0.5),
    shearX: valueAt(k.shear_x, at, 0),
    shearY: valueAt(k.shear_y, at, 0),
    margin: valueAt(k.margin, at, 0),
  }, clip.maxScale, clip.kind === 'image' || clip.kind === 'title');
  const opacity = clamp(valueAt(k.alpha, at, 1), 0, 1) * transitionOpacity(transitions, from + frame);
  const radius = valueAt(k.corner_radius, at, 0);
  const style: React.CSSProperties = {
    position: 'absolute',
    left: 0,
    top: 0,
    width: clip.sourceWidth,
    height: clip.sourceHeight,
    transformOrigin: '0 0',
    transform: cssMatrix(matrix),
    opacity,
    objectFit: 'fill',
    filter: filterAt(clip.filters, at),
    clipPath: cropAt(clip.crop, at),
    mixBlendMode: (clip.blendMode ?? undefined) as React.CSSProperties['mixBlendMode'],
    borderRadius: radius > 0 ? radius : undefined,
  };

  if (clip.kind === 'image' || clip.kind === 'title') {
    return opacity > 0 ? <Img src={src} style={style} /> : null;
  }
  if (time.mode === 'map' || time.mode === 'freeze') {
    // One source frame per output frame (holds, reverse, speed ramps); picture only.
    const source = time.mode === 'map'
      ? time.map[clamp(frame + trim - time.forTrim, 0, time.map.length - 1)]
      : time.trimBefore;
    return (
      <Freeze frame={0}>
        <OffthreadVideo src={src} style={style} trimBefore={source} muted transparent={clip.transparent} />
      </Freeze>
    );
  }
  return (
    <OffthreadVideo
      src={src}
      style={style}
      trimBefore={sourceStart > 0 ? sourceStart : undefined}
      playbackRate={rate}
      muted={!clip.hasAudio}
      volume={volumeAt}
      transparent={clip.transparent}
    />
  );
};
