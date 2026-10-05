import React from 'react';

export type FancyProps = {color?: string};

export function Original({color}: FancyProps) {
  return <div style={{color}} />;
}
