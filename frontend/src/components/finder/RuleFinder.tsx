import { Fragment, useEffect, useRef } from 'react';
import type { ChatClient } from '../../api/client';
import type { BankEntry, EntrySummary, FinderOverview, ProposedScenario, RulePeriod } from '../../api/types';
import { useFinder } from '../../hooks/useFinder';
import { DECISION_BADGE, DECISION_LABEL, STATUS_BADGE, STATUS_LABEL } from '../../lib/finder';
import { formatInteger, formatPercent, formatTimes } from '../../lib/format';
import { safeBlock, safeText } from '../../lib/text';
import { ChevronIcon } from '../icons';
import { TableScroll } from '../TableScroll';
import { DeleteButton } from './DeleteButton';
import { FinderEntry } from './FinderEntry';
import { RunProgressPanel } from './RunProgressPanel';

function plural(count: number, noun: string, many = `${noun}s`): string {
  return `${formatInteger(count)} ${count === 1 ? noun : many}`;
}

function confirmationText(period: RulePeriod | null): string {
  if (period === null) return '–';
  if (period.precision === null) return 'nothing labelled';
  const lift = period.lift === null ? '–' : formatTimes(period.lift);
  return `lift ${lift}; ${formatInteger(period.true_positives)} of ${formatInteger(period.flagged_labelled)}`;
}

interface ScenarioProps {
  scenario: ProposedScenario;
  /** This scenario is the one being processed. */
  processing: boolean;
  /** Something is in progress: nothing else can start. */
  busy: boolean;
  processRequests: number;
  onProcess: () => void;
  onDiscard: () => void;
  onDelete: () => void;
}

/** A proposed scenario: what the model thinks fraud could look like, before any rule is written. */
function ScenarioCard({ scenario, processing, busy, processRequests, onProcess, onDiscard, onDelete }: ScenarioProps) {
  const title = safeText(scenario.title, 120);
  return (
    <li className="panel scenario" aria-label={`Proposed scenario: ${title}`}>
      <h4>{title}</h4>
      <div className="model-text">
        <p className="model-text-label">
          <span className="badge badge-muted">Model text</span> Written from the schema alone. No rule has
          been written and nothing has been run.
        </p>
        <p>
          <strong>How the fraud would show up.</strong> {safeBlock(scenario.hypothesis)}
        </p>
        {scenario.columns.length > 0 && (
          <p>
            <strong>Columns.</strong> <span translate="no">{safeText(scenario.columns.join(', '))}</span>
          </p>
        )}
        <p>
          <strong>Assumptions.</strong>
        </p>
        {scenario.assumptions.length === 0 ? (
          <p>None stated.</p>
        ) : (
          <ul>
            {scenario.assumptions.map((item, i) => (
              <li key={i}>{safeBlock(item)}</li>
            ))}
          </ul>
        )}
      </div>
      {processing ? (
        <p className="scenario-state">Being processed: see the steps above.</p>
      ) : (
        <div className="scenario-actions">
          <button
            type="button"
            className="btn btn-solid"
            disabled={busy}
            aria-label={`Process scenario: ${title}`}
            onClick={onProcess}
          >
            Process
          </button>
          <button
            type="button"
            className="btn btn-outline"
            disabled={busy}
            aria-label={`Discard scenario: ${title}`}
            onClick={onDiscard}
          >
            Discard
          </button>
          <DeleteButton subject={`scenario: ${title}`} disabled={busy} onDelete={onDelete} />
          <p className="scenario-cost">
            Process calls the model: it writes the rule ({plural(processRequests, 'request')} at most), then
            the rule is checked and backtested. Discard keeps the scenario out without a model call.
          </p>
        </div>
      )}
    </li>
  );
}

interface TableProps {
  entries: EntrySummary[];
  overview: FinderOverview;
  client: ChatClient;
  openId: number | null;
  onOpen: (id: number | null) => void;
  onDecided: (entry: BankEntry) => void;
  onDelete: (id: number) => void;
}

function BankTable({ entries, overview, client, openId, onOpen, onDecided, onDelete }: TableProps) {
  const opened = entries.find((entry) => entry.id === openId);
  const panel = useRef<HTMLElement>(null);
  const openedId = opened?.id;
  // The evidence opens under the table: bring it into view, without animation.
  useEffect(() => {
    if (openedId !== undefined) panel.current?.scrollIntoView?.({ block: 'start' });
  }, [openedId]);
  return (
    <>
    <TableScroll className="bank" label="Ranked rules">
      <table>
        <thead>
          <tr>
            <th scope="col">Rule</th>
            <th scope="col">Status</th>
            <th scope="col">Review</th>
            <th scope="col" className="num">Flagged</th>
            <th scope="col" className="num">Fraud caught</th>
            <th scope="col" className="num">Genuine flagged</th>
            <th scope="col" className="num">Precision</th>
            <th scope="col" className="num">Recall</th>
            <th scope="col" className="num">Lift</th>
            <th scope="col">Confirmation</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => {
            const found = entry.discovery;
            const expanded = openId === entry.id;
            return (
              <Fragment key={entry.id}>
                <tr data-status={entry.status} data-open={expanded || undefined}>
                  <th scope="row">
                    <button
                      type="button"
                      className="disclosure bank-open"
                      aria-expanded={expanded}
                      aria-controls={`entry-${entry.id}`}
                      onClick={() => onOpen(expanded ? null : entry.id)}
                    >
                      <ChevronIcon className="disclosure-chevron" />
                      <span>{safeText(entry.title, 120)}</span>
                    </button>
                  </th>
                  <td>
                    <span className={`badge ${STATUS_BADGE[entry.status]}`}>{STATUS_LABEL[entry.status]}</span>
                    <span className="bank-why">{entry.verdict_short}</span>
                  </td>
                  <td>
                    {found === null ? (
                      '–'
                    ) : (
                      <span className={`badge ${DECISION_BADGE[entry.decision]}`}>
                        {DECISION_LABEL[entry.decision]}
                      </span>
                    )}
                  </td>
                  {found === null ? (
                    <td colSpan={6} className="bank-none">
                      No rule to measure
                    </td>
                  ) : (
                    <>
                      <td className="num">{formatInteger(found.flagged)}</td>
                      <td className="num">{formatInteger(found.true_positives)}</td>
                      <td className="num">{formatInteger(found.false_positives)}</td>
                      <td className="num">{formatPercent(found.precision)}</td>
                      <td className="num">{formatPercent(found.recall)}</td>
                      <td className="num">{found.lift === null ? '–' : formatTimes(found.lift)}</td>
                    </>
                  )}
                  <td>{confirmationText(entry.confirmation)}</td>
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </TableScroll>
    {/* Outside the table, which scrolls sideways: the evidence is as wide as the page, never wider. */}
    {opened && (
      <section ref={panel} className="panel entry-panel" id={`entry-${opened.id}`} aria-label={`Evidence: ${safeText(opened.title, 120)}`}>
        <header className="entry-panel-head">
          <h4>{safeText(opened.title, 120)}</h4>
          <button type="button" className="btn btn-ghost btn-small" onClick={() => onOpen(null)}>
            Close
          </button>
        </header>
        <FinderEntry
          key={opened.id}
          id={opened.id}
          client={client}
          note={overview.note}
          onDecided={onDecided}
          onDelete={onDelete}
        />
      </section>
    )}
    </>
  );
}

interface Props {
  client: ChatClient;
  /** The offline demo proposes canned scenarios; the tab says so. */
  demo: boolean;
  /** Milliseconds between two looks at a run in progress. Tests shorten it. */
  pollMs?: number;
}

/**
 * The "Rule finder" tab, in two steps the reviewer starts: find a scenario,
 * then process the ones worth a rule. Below them, the ranked bank, a rule's
 * evidence, and approve or reject.
 */
export function RuleFinder({ client, demo, pollMs }: Props) {
  const finder = useFinder(client, pollMs);
  const { overview, failure, actionFailure, proposing, progress, busy } = finder;
  const entries = overview?.entries ?? [];
  const proposed = overview?.proposed ?? [];
  const confirmed = entries.filter((entry) => entry.status === 'confirmed').length;
  const approved = entries.filter((entry) => entry.decision === 'approved').length;
  const lastRun = overview?.last_run;

  return (
    <section className="finder" aria-labelledby="finder-title">
      <header className="finder-head">
        <h2 id="finder-title">Rule finder</h2>
        <p className="finder-lede">
          Two steps, each started by you. <strong>Find a rule</strong> asks the model for one fraud scenario,
          written from the schema alone, and stops: you read the hypothesis first. <strong>Process</strong>{' '}
          then turns a scenario you chose into a rule, which is checked by the SQL guard, found on January to
          July and confirmed on August to October. Nothing is deployed: you approve or reject each rule.
        </p>
        <div className="finder-actions">
          <button type="button" className="btn btn-solid" onClick={finder.propose} disabled={busy || !overview}>
            {proposing ? 'Finding a rule…' : 'Find a rule'}
          </button>
          {overview && (
            <p className="finder-cost">
              One click asks for one scenario: {plural(overview.propose_requests, 'model request')}, and no
              SQL is written or run.{' '}
              {demo ? 'Offline demo: the scenarios and clauses are canned, the backtest is real.' : 'It is billed.'}
            </p>
          )}
        </div>
      </header>

      <div role="status" aria-live="polite">
        {proposing && <p className="finder-running">Asking the model for a scenario. This takes a few seconds.</p>}
        {!proposing && !progress && overview?.running && (
          <p className="finder-running">The rule finder is busy with a request started elsewhere.</p>
        )}
      </div>
      {failure && (
        <p className="finder-error" role="alert">
          {failure.message}{' '}
          <button type="button" className="btn btn-ghost btn-small" onClick={finder.refresh}>
            Retry
          </button>
        </p>
      )}
      {actionFailure && (
        <p className="finder-error" role="alert">
          {safeText(actionFailure, 400)}
        </p>
      )}
      {lastRun?.status === 'interrupted' && !progress && (
        <p className="finder-none" role="note">
          The last run (run {lastRun.id}) was interrupted: the server stopped before it ended. Nothing was
          lost; its scenario is still waiting below.
        </p>
      )}

      {progress && <RunProgressPanel progress={progress} onDismiss={finder.dismissProgress} />}

      {overview && (
        <section className="bank-section" aria-labelledby="proposed-title">
          <h3 id="proposed-title">Proposed scenarios</h3>
          {proposed.length === 0 ? (
            <p className="finder-empty">
              No scenario is waiting. Find a rule to have one proposed; it will wait here until you process or
              discard it.
            </p>
          ) : (
            <ul className="scenarios">
              {proposed.map((scenario) => (
                <ScenarioCard
                  key={scenario.id}
                  scenario={scenario}
                  processing={progress?.state === 'running' && progress.entry_id === scenario.id}
                  busy={busy}
                  processRequests={overview.process_requests}
                  onProcess={() => finder.process(scenario.id)}
                  onDiscard={() => finder.discard(scenario.id)}
                  onDelete={() => finder.remove(scenario.id)}
                />
              ))}
            </ul>
          )}
          {overview.discarded > 0 && (
            <p className="bank-counts">
              {plural(overview.discarded, 'scenario')} discarded. They are kept so that they are not proposed
              again.
            </p>
          )}
        </section>
      )}

      {overview && (
        <section className="bank-section" aria-labelledby="bank-title">
          <h3 id="bank-title">Rule bank</h3>
          {entries.length === 0 ? (
            <p className="finder-empty">
              The rule bank holds no processed scenario yet. Each one you process is added here, unless its
              rule is already in the bank.
            </p>
          ) : (
            <>
              <p className="bank-counts">
                {plural(entries.length, 'entry', 'entries')}: {confirmed} confirmed, {approved} approved.
                Figures are for the discovery period, the months before {overview.criteria.confirmation_starts}
                ; the confirmation column is the months from it. Open a row for the evidence.
              </p>
              {confirmed === 0 && (
                <p className="finder-none" role="note">
                  No rule in the bank is confirmed. That is a result, not a fault: the scenarios were written
                  without data, and most ways fraud could show up are not how it shows up in this data. The
                  rows below say how far each rule got.
                </p>
              )}
              <BankTable
                entries={entries}
                overview={overview}
                client={client}
                openId={finder.openId}
                onOpen={finder.setOpenId}
                onDecided={finder.decided}
                onDelete={finder.remove}
              />
            </>
          )}
          <p className="finder-note">{overview.note}</p>
        </section>
      )}
      {!overview && !failure && <p className="finder-loading">Loading the rule bank…</p>}
    </section>
  );
}
