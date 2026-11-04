import React from 'react';
import {Composition, Folder, Still} from 'remotion';
import {z} from 'zod';
import {TitleCard} from './TitleCard';
import {Original as Fancy, type FancyProps} from './comps/fancy';
import Promo from './Promo';
import * as Scenes from './scenes';

export const schema = z.object({title: z.string()});

// <Composition id="Ghost" component={TitleCard} />
/* <Composition id="Ghost2" component={TitleCard} durationInFrames={10} /> */

const Thumb: React.FC = () => <div>Don't forget the thumbnail</div>;

export const RemotionRoot: React.FC = () => {
  const items = [1, 2];
  return (
    <>
      <Folder name="Graphics">
        <Composition
          id="TitleCard"
          component={TitleCard}
          durationInFrames={90}
          fps={30}
          width={1920}
          height={1080}
          schema={schema}
          defaultProps={{title: 'Hello {world} />', nested: {a: 1, b: [1, 2, {c: '}'}]}}}
        />
        <Folder name="Lower thirds">
          <Composition id="Aliased" component={Fancy} durationInFrames={30} fps={30} width={1280} height={720} />
        </Folder>
      </Folder>
      <Composition id="Promo" component={Promo} durationInFrames={60} fps={30} width={1080} height={1920} />
      <Composition id={'Outro'} component={Scenes.Outro} durationInFrames={45} fps={30} width={1920} height={1080} />
      <Composition
        id="Lazy"
        lazyComponent={() => import('./Lazy')}
        durationInFrames={20}
        fps={30}
        width={640}
        height={360}
      />
      {items.map((i) => (
        <Composition key={i} id={`dynamic-${i}`} component={TitleCard} durationInFrames={10} fps={30} width={10} height={10} />
      ))}
      <Still id="Thumb" component={Thumb} width={1280} height={720} />
    </>
  );
};
