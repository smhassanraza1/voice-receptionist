import type { ReactNode } from 'react';

/** The clinic mark: a cross over two lake ripples. Same artwork as /favicon.svg. */
export function ClinicMark({ size = 36 }: { size?: number }) {
  return (
    <svg
      className="clinic-mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <rect width="32" height="32" rx="9" fill="var(--accent)" />
      <path d="M14.4 6.5h3.2v3.6h3.6v3.2h-3.6v3.6h-3.2v-3.6h-3.6v-3.2h3.6z" fill="var(--on-accent)" />
      <path
        d="M6.5 21.2c2.4 0 2.4-1.7 4.75-1.7s2.4 1.7 4.75 1.7 2.4-1.7 4.75-1.7 2.4 1.7 4.75 1.7"
        stroke="var(--on-accent)"
        strokeWidth="1.9"
        strokeLinecap="round"
        fill="none"
      />
      <path
        d="M6.5 25.4c2.4 0 2.4-1.7 4.75-1.7s2.4 1.7 4.75 1.7 2.4-1.7 4.75-1.7 2.4 1.7 4.75 1.7"
        stroke="var(--on-accent)"
        strokeWidth="1.9"
        strokeLinecap="round"
        fill="none"
        opacity="0.6"
      />
    </svg>
  );
}

type Section = 'front-desk' | 'operator';

export function SiteHeader({ current, actions }: { current: Section; actions?: ReactNode }) {
  return (
    <header className="site-header">
      <a className="wordmark" href="/" aria-label="Lakeside Family Clinic, front desk">
        <ClinicMark />
        <span className="wordmark__text">
          <span className="wordmark__name">Lakeside</span>
          <span className="wordmark__sub">Family Clinic</span>
        </span>
      </a>
      <nav className="site-nav" aria-label="Sections">
        <a href="/" aria-current={current === 'front-desk' ? 'page' : undefined}>
          Front desk
        </a>
        <a href="/operator" aria-current={current === 'operator' ? 'page' : undefined}>
          Operator console
        </a>
      </nav>
      {actions ? <div className="site-header__actions">{actions}</div> : null}
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <p>
        Lakeside Family Clinic is fictional. This is a portfolio demo of a LiveKit voice agent;
        please don’t share real health information.
      </p>
      <p className="site-footer__meta">
        120 Harbor Street, Suite 200 · Mon–Fri 8–6 · Sat 9–1
      </p>
    </footer>
  );
}
