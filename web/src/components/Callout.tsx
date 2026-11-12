import type { ReactNode } from 'react';
import { AlertIcon, HeadsetIcon, ReceiptIcon } from './Icon.tsx';

type Tone = 'error' | 'info' | 'billing' | 'human';

const ICONS: Record<Tone, ReactNode> = {
  error: <AlertIcon />,
  info: <HeadsetIcon />,
  billing: <ReceiptIcon />,
  human: <HeadsetIcon />,
};

interface CalloutProps {
  tone: Tone;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}

/** A banner with icon + text, so meaning never rests on colour alone. Errors announce assertively. */
export function Callout({ tone, title, children, action }: CalloutProps) {
  return (
    <div className={`callout callout--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <span className="callout__icon">{ICONS[tone]}</span>
      <div className="callout__body">
        <p className="callout__title">{title}</p>
        {children ? <div className="callout__text">{children}</div> : null}
      </div>
      {action ? <div className="callout__action">{action}</div> : null}
    </div>
  );
}
