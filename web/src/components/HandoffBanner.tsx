import type { RemoteParticipant } from 'livekit-client';
import type { HandoffEntry } from '../lib/events.ts';
import { Callout } from './Callout.tsx';

interface HandoffBannerProps {
  handoffs: readonly HandoffEntry[];
  staff: readonly RemoteParticipant[];
}

/** What the caller should know about who they're talking to right now. */
export function HandoffBanner({ handoffs, staff }: HandoffBannerProps) {
  const person = staff[0];
  if (person) {
    const name = person.name || 'A staff member';
    return (
      <Callout tone="human" title={`${name} from the front desk joined the call`}>
        The receptionist has stepped back; you’re talking to a person now.
      </Callout>
    );
  }

  const last = handoffs.at(-1);
  if (!last) return null;
  switch (last.target) {
    case 'billing':
      return (
        <Callout tone="billing" title="Connected to the billing specialist">
          {last.reason || 'Balances, invoices, insurance claims and payment plans.'}
        </Callout>
      );
    case 'human':
      return (
        <Callout tone="human" title="Asking a staff member to join">
          {last.reason ? `${last.reason}. ` : ''}The front desk has been notified; stay on the line.
        </Callout>
      );
    case 'receptionist':
      return handoffs.length > 1 ? (
        <Callout tone="info" title="Back with the receptionist">
          {last.reason || 'Anything else I can help with?'}
        </Callout>
      ) : null;
  }
}
