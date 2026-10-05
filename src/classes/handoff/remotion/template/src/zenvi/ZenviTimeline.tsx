// The Zenvi timeline as one Remotion composition: every clip in a <Sequence>, drawn in Zenvi's order
// (bottom track first, then by position; later ones on top) whatever their order in the file. The
// data is src/zenvi/timeline.json, written by Zenvi.
import React from 'react';
import {AbsoluteFill, Sequence} from 'remotion';
import timeline from './timeline.json';
import {ZenviClip} from './ZenviClip';
import {clipFrames, drawOrder, timelineDurationInFrames} from './timing';
import type {ZenviTimelineData} from './types';

export {clipFrames, timelineDurationInFrames};

export const zenviTimeline = timeline as unknown as ZenviTimelineData;

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
      {drawOrder(zenviTimeline.clips).map((clip) => {
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
