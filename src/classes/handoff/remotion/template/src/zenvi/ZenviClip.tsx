// One Zenvi clip, drawn the way Zenvi (libopenshot 1.0) draws it: the source image placed by the
// clip's scale mode, gravity, location, scale, rotation, origin and shear (geometry.ts), its
// alpha as opacity, its volume curve as Remotion's volume callback, constant speed as
// playbackRate, holds / reverse / ramps frame by frame, and the mapped effects as CSS filters.
import React from 'react';
import {Audio, Freeze, Img, OffthreadVideo, staticFile, useCurrentFrame} from 'remotion';
import {cropAt, filterAt, valueAt} from './curves';
import {clipMatrix, cssMatrix} from './geometry';
import type {ZenviClipData, ZenviTransitionData} from './types';

type Props = {
  clip: ZenviClipData;
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

export const ZenviClip: React.FC<Props> = ({clip, width, height, transitions, volume}) => {
  const frame = useCurrentFrame();
  const k = clip.keyframes;
  const src = staticFile(clip.src);
  const volumeAt = (f: number): number => clamp(valueAt(k.volume, f, 1), 0, 1) * volume;
  const time = clip.time;

  if (clip.kind === 'audio' || !clip.hasVideo) {
    if (!clip.hasAudio) {
      return null;
    }
    const trim = time.mode === 'normal' || time.mode === 'rate' ? time.trimBefore : 0;
    const rate = time.mode === 'normal' || time.mode === 'rate' ? time.playbackRate : 1;
    return <Audio src={src} trimBefore={trim > 0 ? trim : undefined} playbackRate={rate} volume={volumeAt} />;
  }

  const matrix = clipMatrix(clip.sourceWidth, clip.sourceHeight, width, height, clip.scaleMode, clip.gravity, {
    scaleX: valueAt(k.scale_x, frame, 1),
    scaleY: valueAt(k.scale_y, frame, 1),
    locationX: valueAt(k.location_x, frame, 0),
    locationY: valueAt(k.location_y, frame, 0),
    rotation: valueAt(k.rotation, frame, 0),
    originX: valueAt(k.origin_x, frame, 0.5),
    originY: valueAt(k.origin_y, frame, 0.5),
    shearX: valueAt(k.shear_x, frame, 0),
    shearY: valueAt(k.shear_y, frame, 0),
    margin: valueAt(k.margin, frame, 0),
  }, clip.maxScale, clip.kind === 'image' || clip.kind === 'title');
  const opacity = clamp(valueAt(k.alpha, frame, 1), 0, 1) * transitionOpacity(transitions, clip.from + frame);
  const radius = valueAt(k.corner_radius, frame, 0);
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
    filter: filterAt(clip.filters, frame),
    clipPath: cropAt(clip.crop, frame),
    mixBlendMode: (clip.blendMode ?? undefined) as React.CSSProperties['mixBlendMode'],
    borderRadius: radius > 0 ? radius : undefined,
  };

  if (clip.kind === 'image' || clip.kind === 'title') {
    return opacity > 0 ? <Img src={src} style={style} /> : null;
  }
  if (time.mode === 'map' || time.mode === 'freeze') {
    // One source frame per output frame (holds, reverse, speed ramps); picture only.
    const source = time.mode === 'map' ? time.map[clamp(frame, 0, time.map.length - 1)] : time.trimBefore;
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
      trimBefore={time.trimBefore > 0 ? time.trimBefore : undefined}
      playbackRate={time.playbackRate}
      muted={!clip.hasAudio}
      volume={volumeAt}
      transparent={clip.transparent}
    />
  );
};
