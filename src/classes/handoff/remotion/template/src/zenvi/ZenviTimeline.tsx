// The Zenvi timeline as one Remotion composition: every clip in a <Sequence>, bottom track first
// (later tracks draw on top, like Zenvi). The data is src/zenvi/timeline.json, written by Zenvi.
import React from 'react';
import {AbsoluteFill, Sequence} from 'remotion';
import timeline from './timeline.json';
import {ZenviClip} from './ZenviClip';
import type {ZenviClipData, ZenviTimelineData} from './types';

export const zenviTimeline = timeline as unknown as ZenviTimelineData;

// Frames from seconds, rounded half up -- exactly as Zenvi's exporter does.
const toFrame = (seconds: number, fps: number): number => Math.floor(seconds * fps + 0.5);

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

// The props Root.tsx's zod schema describes (editable in Remotion Studio).
export type ZenviTimelineProps = {
  background: string;
  volume: number;
  showTitles: boolean;
};

export const ZenviTimeline: React.FC<ZenviTimelineProps> = ({background, volume, showTitles}) => {
  const {width, height, fps} = zenviTimeline.composition;
  return (
    <AbsoluteFill style={{backgroundColor: background, overflow: 'hidden'}}>
      {zenviTimeline.clips.map((clip) => {
        if (!showTitles && clip.kind === 'title') {
          return null;
        }
        const {from, durationInFrames} = clipFrames(clip, fps);
        const transitions = zenviTimeline.transitions.filter((t) => t.layer === clip.layer);
        return (
          <Sequence key={clip.id} from={from} durationInFrames={durationInFrames} name={clip.title || clip.id}>
            <ZenviClip clip={clip} from={from} fps={fps} width={width} height={height} transitions={transitions}
              volume={volume} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
