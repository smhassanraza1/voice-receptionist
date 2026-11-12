import { lazy, Suspense } from 'react';

// Two pages sharing one bundle setup: the caller page and the operator console. The server (and
// Vite in dev) serve index.html for both paths; each page loads as its own chunk.
const CallerPage = lazy(() => import('./pages/CallerPage.tsx').then((m) => ({ default: m.CallerPage })));
const OperatorPage = lazy(() =>
  import('./pages/OperatorPage.tsx').then((m) => ({ default: m.OperatorPage })),
);

export function App({ pathname }: { pathname: string }) {
  const path = pathname.replace(/\/+$/, '') || '/';
  const Page = path === '/operator' ? OperatorPage : CallerPage;
  return (
    <Suspense fallback={<div className="boot" aria-busy="true" />}>
      <Page />
    </Suspense>
  );
}
