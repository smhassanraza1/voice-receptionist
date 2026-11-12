import { type ReactNode, useId } from 'react';
import {
  ARCHITECTURES,
  type CallConfig,
  LANGUAGES,
  type Language,
  type Mode,
} from '../lib/config.ts';

interface CallSetupProps {
  config: CallConfig;
  onChange: (config: CallConfig) => void;
  disabled?: boolean;
}

function Fieldset({ legend, hint, children }: { legend: string; hint?: string; children: ReactNode }) {
  const hintId = useId();
  return (
    <fieldset className="field" aria-describedby={hint ? hintId : undefined}>
      <legend className="field__legend">{legend}</legend>
      {hint ? (
        <p id={hintId} className="field__hint">
          {hint}
        </p>
      ) : null}
      {children}
    </fieldset>
  );
}

/** Native radio inputs styled as cards and chips: keyboard and screen readers work for free. */
export function CallSetup({ config, onChange, disabled = false }: CallSetupProps) {
  const name = useId();
  const set = (patch: Partial<CallConfig>) => onChange({ ...config, ...patch });

  return (
    <div className="call-setup">
      <Fieldset legend="Architecture">
        <div className="arch-options">
          {ARCHITECTURES.map((arch) => (
            <label key={arch.value} className="arch-option">
              <input
                type="radio"
                name={`${name}-mode`}
                value={arch.value}
                checked={config.mode === arch.value}
                disabled={disabled}
                aria-labelledby={`${name}-${arch.value}-title`}
                aria-describedby={`${name}-${arch.value}-blurb`}
                onChange={() => set({ mode: arch.value as Mode })}
              />
              <span id={`${name}-${arch.value}-title`} className="arch-option__title">
                {arch.label}
              </span>
              <span id={`${name}-${arch.value}-blurb`} className="arch-option__blurb">
                {arch.blurb}
              </span>
            </label>
          ))}
        </div>
      </Fieldset>

      <Fieldset legend="Language" hint="Auto follows whatever language you speak.">
        <div className="chips">
          {LANGUAGES.map((language) => (
            <label key={language.value} className="chip">
              <input
                type="radio"
                name={`${name}-language`}
                value={language.value}
                checked={config.language === language.value}
                disabled={disabled}
                onChange={() => set({ language: language.value as Language })}
              />
              <span lang={language.lang}>{language.label}</span>
            </label>
          ))}
        </div>
      </Fieldset>
    </div>
  );
}
