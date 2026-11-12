import { type FormEvent, useEffect, useId, useState } from 'react';
import { Callout } from '../components/Callout.tsx';
import { LockIcon, SignOutIcon, Spinner } from '../components/Icon.tsx';
import { SiteFooter, SiteHeader } from '../components/SiteChrome.tsx';
import { type FeedStatus, shownEscalation, useEscalationFeed } from '../hooks/useEscalationFeed.ts';
import { useNow } from '../hooks/useElapsed.ts';
import { ApiError, type Escalation, errorMessage, listEscalations } from '../lib/api.ts';
import { formatRelative } from '../lib/format.ts';
import { EscalationDetail } from './EscalationDetail.tsx';

export interface OperatorAuth {
  name: string;
  passcode: string;
}

// Per-tab: closing the tab signs the operator out.
const AUTH_KEY = 'lakeside.operator';

function loadAuth(): OperatorAuth | null {
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(AUTH_KEY) ?? 'null');
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      typeof (parsed as OperatorAuth).name === 'string' &&
      typeof (parsed as OperatorAuth).passcode === 'string'
    ) {
      return parsed as OperatorAuth;
    }
  } catch {
    // Storage unavailable or corrupted: sign in again.
  }
  return null;
}

function saveAuth(auth: OperatorAuth | null): void {
  try {
    if (auth) window.sessionStorage.setItem(AUTH_KEY, JSON.stringify(auth));
    else window.sessionStorage.removeItem(AUTH_KEY);
  } catch {
    // Not persisted; the operator stays signed in for this page view only.
  }
}

export function OperatorPage() {
  const [auth, setAuth] = useState<OperatorAuth | null>(loadAuth);
  const [notice, setNotice] = useState<string | null>(null);
  const [inCall, setInCall] = useState(false);

  const signIn = (next: OperatorAuth) => {
    saveAuth(next);
    setNotice(null);
    setAuth(next);
  };
  const signOut = (reason: string | null = null) => {
    saveAuth(null);
    setNotice(reason);
    setAuth(null);
  };

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <SiteHeader
        current="operator"
        actions={
          auth ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => signOut()}
              disabled={inCall}
              title={inCall ? 'Leave the call before signing out' : undefined}
            >
              <SignOutIcon size={16} /> Sign out
            </button>
          ) : null
        }
      />
      {auth ? (
        <OperatorConsole
          auth={auth}
          inCall={inCall}
          onInCallChange={setInCall}
          onUnauthorized={() => signOut('The passcode changed or is no longer valid. Sign in again.')}
        />
      ) : (
        <SignIn onSignedIn={signIn} notice={notice} />
      )}
      <SiteFooter />
    </div>
  );
}

function SignIn({ onSignedIn, notice }: { onSignedIn: (auth: OperatorAuth) => void; notice: string | null }) {
  const [name, setName] = useState('');
  const [passcode, setPasscode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const errorId = useId();

  useEffect(() => {
    document.title = 'Operator sign-in · Lakeside Family Clinic';
  }, []);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await listEscalations(passcode); // validates the passcode
      onSignedIn({ name: name.trim(), passcode });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401 ? 'That passcode isn’t right.' : errorMessage(err),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main id="main" className="signin-layout">
      <form className="card signin" onSubmit={(e) => void submit(e)} aria-describedby={error ? errorId : undefined}>
        <span className="signin__icon" aria-hidden="true">
          <LockIcon size={22} />
        </span>
        <h1 className="display display--sm">Staff sign-in</h1>
        <p className="lede">
          Pick up calls the receptionist hands to a person. The passcode is the server’s{' '}
          <code>OPERATOR_PASSCODE</code>.
        </p>
        {notice ? <Callout tone="info" title={notice} /> : null}
        <label className="text-field">
          <span className="text-field__label">Your name</span>
          <input
            type="text"
            name="name"
            autoComplete="name"
            required
            maxLength={40}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Dana Whitfield"
          />
          <span className="text-field__hint">Callers hear and see this name.</span>
        </label>
        <label className="text-field">
          <span className="text-field__label">Passcode</span>
          <input
            type="password"
            name="passcode"
            autoComplete="current-password"
            required
            value={passcode}
            onChange={(e) => setPasscode(e.target.value)}
          />
        </label>
        {error ? (
          <p id={errorId} className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn--primary" disabled={busy || !name.trim() || !passcode}>
          {busy ? <Spinner /> : null} Sign in
        </button>
      </form>
    </main>
  );
}

const FEED_LABEL: Record<FeedStatus, string> = {
  connecting: 'Connecting',
  live: 'Live',
  reconnecting: 'Reconnecting',
  unauthorized: 'Signed out',
  disabled: 'Disabled',
};

interface OperatorConsoleProps {
  auth: OperatorAuth;
  inCall: boolean;
  onInCallChange: (inCall: boolean) => void;
  onUnauthorized: () => void;
}

function OperatorConsole({ auth, inCall, onInCallChange, onUnauthorized }: OperatorConsoleProps) {
  const feed = useEscalationFeed(auth.passcode);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [lastShown, setLastShown] = useState<Escalation | null>(null);
  const now = useNow(15_000);

  const open = feed.escalations.filter((e) => e.status === 'open');
  const resolved = feed.escalations.filter((e) => e.status === 'resolved').slice(0, 10);
  const selected = shownEscalation(feed.escalations, selectedId, lastShown, inCall);
  if (selected !== lastShown) setLastShown(selected);

  useEffect(() => {
    // Signing out unmounts the console; an operator on a call keeps it until they leave.
    if (feed.status === 'unauthorized' && !inCall) onUnauthorized();
  }, [feed.status, inCall, onUnauthorized]);

  useEffect(() => {
    document.title = `${open.length > 0 ? `(${open.length}) ` : ''}Operator console · Lakeside Family Clinic`;
  }, [open.length]);

  const renderItem = (item: Escalation) => {
    const isNew = item.status === 'open' && now - new Date(item.created_at).getTime() < 120_000;
    return (
      <li key={item.id}>
        <button
          type="button"
          className="queue-item"
          aria-current={item.id === selectedId ? 'true' : undefined}
          disabled={inCall && item.id !== selectedId}
          onClick={() => setSelectedId(item.id)}
        >
          <span className={`queue-item__status queue-item__status--${item.status}`} aria-hidden="true" />
          <span className="queue-item__reason">{item.reason}</span>
          <span className="queue-item__meta">
            <code>{item.room}</code>
            <span>{formatRelative(item.created_at, now)}</span>
            {isNew ? <span className="badge badge--accent">New</span> : null}
            {item.status === 'resolved' ? <span className="badge">Resolved</span> : null}
          </span>
        </button>
      </li>
    );
  };

  return (
    <main id="main" className="operator-layout">
      <section className="card queue" aria-labelledby="queue-title">
        <header className="card__header">
          <h1 id="queue-title" className="eyebrow">
            Waiting for a person <span className="eyebrow__aside">{open.length}</span>
          </h1>
          <output className={`feed-status feed-status--${feed.status}`}>
            <span className="feed-status__dot" aria-hidden="true" />
            {FEED_LABEL[feed.status]}
          </output>
        </header>
        {feed.status === 'disabled' ? (
          <Callout tone="error" title="The operator console is disabled on the server">
            Set <code>OPERATOR_PASSCODE</code> for <code>python -m receptionist.web</code>.
          </Callout>
        ) : null}
        {open.length === 0 ? (
          <p className="empty-hint">
            Nobody is waiting. When a browser caller asks for a person, the agent files an
            escalation and it appears here straight away.
          </p>
        ) : (
          <ul className="queue__list" aria-label="Open escalations">
            {open.map(renderItem)}
          </ul>
        )}
        {inCall ? <p className="queue__lock">Leave the current call to pick up another.</p> : null}
        {resolved.length > 0 ? (
          <details className="queue__resolved">
            <summary>Recently resolved ({resolved.length})</summary>
            <ul className="queue__list" aria-label="Resolved escalations">
              {resolved.map(renderItem)}
            </ul>
          </details>
        ) : null}
      </section>

      {selected ? (
        <EscalationDetail
          key={selected.id}
          escalation={selected}
          auth={auth}
          onUpdated={feed.upsert}
          onInCallChange={onInCallChange}
          onClose={() => setSelectedId(null)}
        />
      ) : (
        <section className="card detail-placeholder" aria-label="No escalation selected">
          <p className="display display--sm">Pick a caller from the queue.</p>
          <p className="lede">
            You’ll see why the receptionist escalated and its summary of the call so far. Join to
            talk to the caller directly; the agent goes quiet as soon as you’re in the room.
          </p>
        </section>
      )}
    </main>
  );
}
