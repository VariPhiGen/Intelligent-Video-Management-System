/**
 * Stepper.tsx — the wizard step rail. The step SET is passed in, because the
 * flow differs by method: ONVIF discovery needs Credentials + Assign, but a
 * direct add (manual RTSP / CSV) registers the camera immediately and skips
 * both. Badges are sequential over whatever steps are shown. Clicking a pill
 * only goes back to an already-completed step.
 */
import { Fragment } from 'react';

export interface WizardStep { n: number; label: string }

export function Stepper({ steps, current, onStepClick }: {
  steps: WizardStep[];
  current: number;
  onStepClick: (n: number) => void;
}) {
  const curIdx = steps.findIndex(s => s.n === current);
  return (
    <div className="wz-steps">
      {steps.map((s, i) => {
        const active = s.n === current;
        const done = i < curIdx;
        return (
          <Fragment key={s.n}>
            {i > 0 && <div className="wz-line" />}
            <div
              className={`wz-step${active ? ' active' : ''}${done ? ' done' : ''}`}
              onClick={() => { if (done) onStepClick(s.n); }}
            >
              <div className="wz-num">{i === steps.length - 1 ? '✓' : i + 1}</div>
              <span>{s.label}</span>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
