import React from 'react';
import {AbsoluteFill} from 'remotion';

// The title card.
export const TitleCard: React.FC<{title: string}> = ({title}) => {
  return <AbsoluteFill>{title}</AbsoluteFill>;
};
