import { MediaDeviceFailure } from 'livekit-client';
import { ApiError } from './api.ts';

/** A sentence a caller can act on, for anything `session.start()` can throw. */
export function describeStartError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  switch (MediaDeviceFailure.getFailure(error)) {
    case MediaDeviceFailure.PermissionDenied:
      return 'Microphone access is blocked. Allow it for this site in your browser settings, then try again.';
    case MediaDeviceFailure.NotFound:
      return 'No microphone was found. Plug one in or pick another input device, then try again.';
    case MediaDeviceFailure.DeviceInUse:
      return 'Another app is using your microphone. Close it and try again.';
    default:
      break;
  }
  const message = error instanceof Error ? error.message : '';
  if (/agent/i.test(message)) {
    return 'The receptionist didn’t pick up. Check that the agent worker is running (python -m receptionist.agent dev).';
  }
  return message ? `The call couldn’t connect: ${message}` : 'The call couldn’t connect. Try again.';
}
