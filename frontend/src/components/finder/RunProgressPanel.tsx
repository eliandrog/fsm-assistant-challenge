import type { RunProgress } from '../../api/types';
import { STEPS } from '../../lib/finder';
import { safeBlock, safeText } from '../../lib/text';
import { SqlCode } from '../SqlCode';

type StepState = 'done' | 'current' | 'pending' | 'unreached';

const STATE_TEXT: Record<StepState, string> = {
  done: 'Done',
  current: 'In progress',
  pending: 'Waiting',
  unreached: 'Not reached',
};

/** How the run ended, in the backend's own sentence, and where to look. */
function ending(progress: RunProgress): string {
  if (progress.state === 'running') return '';
  if (progress.outcome === 'duplicate') {
    return `${progress.verdict ?? `This matched rule ${progress.result_entry_id}.`} Rule ${progress.result_entry_id} is open below.`;
  }
  if (progress.outcome === 'added') {
    const where = progress.status === 'not_built' ? 'The entry is open below.' : 'The rule and its evidence are open below.';
    return `${progress.verdict ?? 'The scenario was processed.'} ${where}`;
  }
  return progress.error?.message ?? 'The run ended without a result.';
}

interface Props {
  progress: RunProgress;
  onDismiss: () => void;
}

/** One scenario being turned into a rule, step by step, and then how it ended. */
export function RunProgressPanel({ progress, onDismiss }: Props) {
  const running = progress.state === 'running';
  const failed = !running && progress.outcome === null;
  const stateOf = (name: (typeof STEPS)[number]['name']): StepState => {
    if (progress.done.includes(name)) return 'done';
    if (running) return progress.step === name ? 'current' : 'pending';
    return 'unreached';
  };

  return (
    <section className="panel progress" aria-label="Processing a scenario" data-state={progress.state}>
      <header className="progress-head">
        <h3>
          {running ? 'Processing' : failed ? 'Processing stopped' : 'Processed'}: “
          {safeText(progress.scenario.title, 120)}”
        </h3>
        {!running && (
          <button type="button" className="btn btn-ghost btn-small" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </header>
      <p className="progress-hypothesis">{safeBlock(progress.scenario.hypothesis)}</p>

      <ol className="steps">
        {STEPS.map(({ name, label }) => {
          const state = stateOf(name);
          return (
            <li key={name} data-state={state} aria-current={state === 'current' ? 'step' : undefined}>
              <span className="step-mark" aria-hidden="true">
                {state === 'done' ? '✓' : state === 'current' ? '' : '·'}
              </span>
              <span>
                {label}
                {name === 'write_rule' && progress.attempt > 1 && ' — second attempt, after the first was refused or failed'}
              </span>
              <span className="step-state">{STATE_TEXT[state]}</span>
            </li>
          );
        })}
      </ol>

      {progress.clause !== null && (
        <div className="progress-clause">
          <p className="panel-title">The clause the guard accepted</p>
          <SqlCode sql={safeBlock(progress.clause)} label="Rule WHERE clause" />
        </div>
      )}

      {/* Polite: a screen reader hears the step change and the ending, not every poll. */}
      <p className={failed ? 'finder-error' : 'progress-result'} role="status" aria-live="polite">
        {running ? (
          `${STEPS.find((step) => step.name === progress.step)?.label ?? 'Working'}…`
        ) : (
          <>
            {safeBlock(ending(progress))}
          </>
        )}
      </p>
    </section>
  );
}
