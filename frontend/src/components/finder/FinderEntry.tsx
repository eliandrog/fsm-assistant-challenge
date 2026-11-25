import { useEffect, useId, useState } from 'react';
import type { ChatClient } from '../../api/client';
import type { BankEntry, ClientFailure, Criterion, Decision } from '../../api/types';
import {
  CAVEAT_TITLE,
  DECISION_BADGE,
  DECISION_LABEL,
  OUTCOME_LABEL,
  STATUS_BADGE,
  STATUS_LABEL,
  STATUS_MEANING,
  formatClause,
  formatStamp,
  shownCaveats,
} from '../../lib/finder';
import { safeBlock, safeText } from '../../lib/text';
import { CopyButton } from '../CopyButton';
import { AlertIcon } from '../icons';
import { SqlCode } from '../SqlCode';
import { RuleCard } from '../RuleCard';
import { DeleteButton } from './DeleteButton';
import { SqlBlock } from '../SqlBlock';

const NOTE_LIMIT = 1000;

function Checks({ name, checks }: { name: string; checks: Criterion[] }) {
  return (
    <div className="checks">
      <h5>{name}</h5>
      <ul>
        {checks.map((check) => (
          <li key={check.code} data-passed={check.passed}>
            <span className={`badge ${check.passed ? 'badge-ok' : 'badge-warn'}`}>
              {check.passed ? 'Met' : 'Not met'}
            </span>{' '}
            {check.detail}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Assumptions({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <p>
        <strong>{title}</strong>
      </p>
      {items.length === 0 ? (
        <p>None stated.</p>
      ) : (
        <ul>
          {items.map((item, i) => (
            <li key={i}>{safeBlock(item)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface ReviewProps {
  entry: BankEntry;
  client: ChatClient;
  onDecided: (entry: BankEntry) => void;
}

/** Approve or reject, with an optional note. The decision is saved in the rule bank. */
function Review({ entry, client, onDecided }: ReviewProps) {
  const [note, setNote] = useState(entry.decision_note ?? '');
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<ClientFailure | null>(null);
  const noteId = useId();

  const decide = (decision: Decision) => {
    setSaving(true);
    setFailure(null);
    void client.finderDecide(entry.id, decision, decision === 'pending' ? '' : note).then((found) => {
      setSaving(false);
      if (found.kind === 'ok') onDecided(found.value);
      else setFailure(found.failure);
    });
  };

  return (
    <section className="review" aria-label="Review decision">
      <h4>Review</h4>
      <p className="review-state" role="status">
        <span className={`badge ${DECISION_BADGE[entry.decision]}`}>{DECISION_LABEL[entry.decision]}</span>
        {entry.decided_at && <span> on {formatStamp(entry.decided_at)}</span>}
        {entry.decision_note && <span>: {safeBlock(entry.decision_note)}</span>}
      </p>
      <label htmlFor={noteId}>Note (optional)</label>
      <textarea
        id={noteId}
        value={note}
        maxLength={NOTE_LIMIT}
        rows={2}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Why this rule holds, or why it does not"
      />
      <div className="review-actions">
        <button type="button" className="btn btn-solid" disabled={saving} onClick={() => decide('approved')}>
          Approve
        </button>
        <button type="button" className="btn btn-outline" disabled={saving} onClick={() => decide('rejected')}>
          Reject
        </button>
        {entry.decision !== 'pending' && (
          <button type="button" className="btn btn-ghost" disabled={saving} onClick={() => decide('pending')}>
            Back to pending
          </button>
        )}
      </div>
      {failure && (
        <p className="finder-error" role="alert">
          The decision was not saved. {failure.message}
        </p>
      )}
      <p className="review-hint">
        Approving records a reviewer’s judgement in the rule bank. It deploys nothing.
      </p>
    </section>
  );
}

interface Props {
  id: number;
  client: ChatClient;
  /** What the two-period check is and is not; shown under the month table. */
  note: string;
  onDecided: (entry: BankEntry) => void;
  onDelete: (id: number) => void;
}

/** One entry of the rule bank in full: the scenario, what was assumed, the rule and its evidence. */
export function FinderEntry({ id, client, note, onDecided, onDelete }: Props) {
  const [entry, setEntry] = useState<BankEntry | null>(null);
  const [failure, setFailure] = useState<ClientFailure | null>(null);

  useEffect(() => {
    let alive = true;
    void client.finderEntry(id).then((found) => {
      if (!alive) return;
      if (found.kind === 'ok') setEntry(found.value);
      else setFailure(found.failure);
    });
    return () => {
      alive = false;
    };
  }, [client, id]);

  if (failure) {
    return (
      <p className="finder-error" role="alert">
        This entry could not be loaded. {failure.message}
      </p>
    );
  }
  if (!entry) return <p className="finder-loading">Loading the evidence…</p>;

  const discovery = entry.criteria.filter((check) => check.period === 'discovery');
  const confirmation = entry.criteria.filter((check) => check.period === 'confirmation');
  const saved = (decided: BankEntry) => {
    setEntry(decided);
    onDecided(decided);
  };

  const caveats = shownCaveats(entry);

  return (
    <div className="entry">
      {/* First, what a reviewer asks first: what happened, and what rule was tested. */}
      <section className="entry-block verdict" aria-label="Verdict">
        <h4>
          <span className={`badge ${STATUS_BADGE[entry.status]}`}>{STATUS_LABEL[entry.status]}</span> What
          happened
        </h4>
        <p className="verdict-sentence">{safeBlock(entry.verdict)}</p>
        {entry.clause === null && entry.turn_outcome !== null && (
          <>
            {entry.turn_message && (
              <p className="entry-aside">
                {OUTCOME_LABEL[entry.turn_outcome]}: {safeBlock(entry.turn_message)}
              </p>
            )}
            {entry.refused.length > 0 && (
              <p className="entry-aside">
                Replies refused: <span translate="no">{entry.refused.join(', ')}</span>
              </p>
            )}
          </>
        )}
      </section>

      {entry.clause !== null && (
        <section className="panel rule-tested" aria-label="The rule that was tested">
          <header className="panel-bar">
            <span className="panel-title">The rule: the WHERE clause that was tested</span>
            <CopyButton text={entry.clause} label="Copy the rule" />
          </header>
          <SqlCode sql={formatClause(safeBlock(entry.clause))} label="The rule's WHERE clause" wrap />
          {entry.explanation && (
            <p className="rule-tested-note">
              <span className="badge badge-muted">Model text</span> {safeBlock(entry.explanation)}
            </p>
          )}
        </section>
      )}

      <section className="model-text" aria-label="Scenario">
        <p className="model-text-label">
          <span className="badge badge-muted">Model text</span> Scenario written by the model from the
          schema alone. It saw no transaction and no result.
        </p>
        <p>
          <strong>Hypothesis.</strong> {safeBlock(entry.hypothesis)}
        </p>
        {entry.columns.length > 0 && (
          <p>
            <strong>Columns.</strong> <span translate="no">{safeText(entry.columns.join(', '))}</span>
          </p>
        )}
      </section>

      <section className="model-text assumptions" aria-label="Assumptions">
        <h4>Assumptions</h4>
        <p className="model-text-label">
          Stated by the model, which saw no data. Check each one: where one does not hold, the evidence
          below does not mean what the scenario says.
        </p>
        <Assumptions title="When writing the scenario" items={entry.scenario_assumptions} />
        {entry.clause !== null && <Assumptions title="When writing the rule" items={entry.rule_assumptions} />}
      </section>

      {entry.criteria.length > 0 && (
        <section className="entry-block" aria-label="Checks">
          <h4>The checks behind the verdict</h4>
          <p>{STATUS_MEANING[entry.status]}</p>
          <div className="checks-grid">
            <Checks name="Discovery period" checks={discovery} />
            <Checks name="Confirmation period" checks={confirmation} />
          </div>
          {entry.duplicate_check === 'incomplete' && (
            <p className="entry-aside">
              The comparison with the rules already in the bank could not be completed, so this rule may
              duplicate one of them.
            </p>
          )}
        </section>
      )}

      {caveats.length > 0 && (
        <ul className="caveats" aria-label="Caveats">
          {caveats.map((caveat) => (
            <li key={caveat.code} className="caveat" data-code={caveat.code}>
              <AlertIcon className="callout-icon" />
              <p>
                <strong className="caveat-title">{CAVEAT_TITLE[caveat.code]}</strong>{' '}
                <span className="caveat-code" translate="no">
                  {caveat.code}
                </span>
                {safeBlock(caveat.message)}
              </p>
            </li>
          ))}
        </ul>
      )}

      {/* Then the detail, closed until asked for. */}
      {entry.rule && entry.clause !== null && (
        <details className="entry-details">
          <summary>Backtest figures, month by month and by period</summary>
          <RuleCard
            rule={entry.rule}
            whereClause={entry.clause}
            periodNames={['Discovery period', 'Confirmation period']}
            monthsHeading="Month by month"
            note={note}
          />
        </details>
      )}

      {entry.backtest_sql && <SqlBlock sql={entry.backtest_sql} title="Backtest SQL, whole history" defaultOpen={false} />}
      {entry.monthly_sql && <SqlBlock sql={entry.monthly_sql} title="Backtest SQL, by month" defaultOpen={false} />}

      {entry.matches.length > 0 && (
        <section className="entry-block" aria-label="Later scenarios that led to this rule">
          <h4>Proposed again since</h4>
          <ul className="entry-matches">
            {entry.matches.map((match, i) => (
              <li key={i}>
                Run {match.run_id}: “{safeText(match.title, 120)}” led to{' '}
                {match.kind === 'same_clause' ? 'the same clause' : 'a clause that flags exactly the same transactions'}
                , so no entry was added.
              </li>
            ))}
          </ul>
        </section>
      )}

      {entry.clause !== null && <Review key={entry.decision} entry={entry} client={client} onDecided={saved} />}

      <section className="entry-block" aria-label="Delete this entry">
        <DeleteButton subject={`entry ${entry.id}`} onDelete={() => onDelete(entry.id)} />
      </section>

      <p className="entry-aside">
        Entry {entry.id}, from run {entry.run_id} on {formatStamp(entry.created_at)}. Model{' '}
        <span translate="no">{safeText(entry.model_id)}</span>; prompts{' '}
        <span translate="no">
          {entry.scenario_prompt_version} and {entry.rule_prompt_version}
        </span>
        .
      </p>
    </div>
  );
}
