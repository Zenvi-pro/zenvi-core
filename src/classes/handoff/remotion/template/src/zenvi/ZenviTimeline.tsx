// The Zenvi timeline as one Remotion composition: every clip in a <Sequence>, bottom track first
// (later tracks draw on top, like Zenvi). The data is src/zenvi/timeline.json, written by Zenvi.
import React from 'react';
import {AbsoluteFill, Sequence} from 'remotion';
import timeline from './timeline.json';
import {ZenviClip} from './ZenviClip';
import type {ZenviTimelineData} from './types';

export const zenviTimeline = timeline as unknown as ZenviTimelineData;

// The props Root.tsx's zod schema describes (editable in Remotion Studio).
export type ZenviTimelineProps = {
  background: string;
  volume: number;
  showTitles: boolean;
};

export const ZenviTimeline: React.FC<ZenviTimelineProps> = ({background, volume, showTitles}) => {
  const {width, height} = zenviTimeline.composition;
  return (
    <AbsoluteFill style={{backgroundColor: background, overflow: 'hidden'}}>
      {zenviTimeline.clips.map((clip) => {
        if (!showTitles && clip.kind === 'title') {
          return null;
        }
        const transitions = zenviTimeline.transitions.filter((t) => t.layer === clip.layer);
        return (
          <Sequence key={clip.id} from={clip.from} durationInFrames={clip.durationInFrames} name={clip.title || clip.id}>
            <ZenviClip clip={clip} width={width} height={height} transitions={transitions} volume={volume} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
