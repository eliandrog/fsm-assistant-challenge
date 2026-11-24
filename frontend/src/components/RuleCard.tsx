import type { RuleCard as Rule, RulePeriod } from '../api/types';
import { EVIDENCE_NOTE } from '../lib/evidence';
import { formatInteger, formatPercent, formatTimes, formatUsdCents, rawTitle } from '../lib/format';
import { safeBlock } from '../lib/text';
import { CopyButton } from './CopyButton';
import { RuleIcon } from './icons';
import { SqlCode } from './SqlCode';
import { TableScroll } from './TableScroll';

function Meter({ value, label }: { value: number | null; label: string }) {
  const pct = value === null ? 0 : Math.max(0, Math.min(1, value)) * 100;
  return (
    <div
      className="meter"
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct * 10) / 10}
      aria-valuetext={formatPercent(value)}
    >
      <div className="meter-fill" style={{ transform: `scaleX(${pct / 100})` }} />
    </div>
  );
}

function span(period: RulePeriod): string {
  if (period.first_month === null || period.last_month === null) return 'no months';
  return period.first_month === period.last_month
    ? period.first_month
    : `${period.first_month} to ${period.last_month}`;
}

function Period({ name, period }: { name: string; period: RulePeriod }) {
  return (
    <div className="period">
      <dt>
        {name} <span className="period-span">{span(period)}</span>
      </dt>
      {period.precision === null ? (
        <dd>Nothing flagged is labelled.</dd>
      ) : (
        <dd>
          <strong>{formatPercent(period.precision)}</strong> precision (
          {formatInteger(period.true_positives)} of {formatInteger(period.flagged_labelled)} labelled
          flagged were fraud); recall {formatInteger(period.true_positives)} of{' '}
          {formatInteger(period.total_fraud)}; lift{' '}
          {period.lift === null ? 'undefined' : formatTimes(period.lift)}
        </dd>
      )}
    </div>
  );
}

interface Props {
  rule: Rule;
  whereClause: string;
}

/** A proposed rule and its backtest. Every figure was computed by the backend from executed SQL. */
export function RuleCard({ rule, whereClause }: Props) {
  const clause = safeBlock(whereClause);
  const { stability } = rule;
  const weakest = rule.months.find((row) => row.month === rule.weakest_month);
  const perCatch = rule.true_positives > 0 ? rule.false_positives / rule.true_positives : null;

  return (
    <section className="panel rule-card" aria-label="Proposed rule">
      <header className="rule-head">
        <span className="rule-eyebrow">
          <RuleIcon /> Proposed rule
        </span>
        <p className="rule-description">A WHERE clause over transactions, backtested on the fraud labels</p>
      </header>

      <div className="rule-clause">
        <div className="rule-clause-bar">
          <span className="panel-title" translate="no">
            WHERE
          </span>
          <CopyButton text={whereClause} label="Copy WHERE clause" />
        </div>
        <SqlCode sql={clause} label="Rule WHERE clause" />
      </div>

      <h3 className="rule-subhead">Backtest on labelled transactions</h3>
      <dl className="metrics">
        <div className="metric">
          <dt>Fraud caught</dt>
          <dd className="metric-value">{formatInteger(rule.true_positives)}</dd>
          <dd className="metric-sub">of {formatInteger(rule.total_fraud)} labelled fraud</dd>
        </div>
        <div className="metric">
          <dt>False positives</dt>
          <dd className="metric-value">{formatInteger(rule.false_positives)}</dd>
          <dd className="metric-sub">legitimate, worth {formatUsdCents(rule.false_positive_amount_cents)}</dd>
        </div>
        <div className="metric">
          <dt>Precision</dt>
          <dd className="metric-value">{formatPercent(rule.precision)}</dd>
          <dd className="metric-sub">
            <Meter value={rule.precision} label="Precision" />
            {rule.precision === null ? 'undefined: nothing flagged is labelled' : 'of labelled flagged are fraud'}
          </dd>
        </div>
        <div className="metric">
          <dt>Recall</dt>
          <dd className="metric-value">{formatPercent(rule.recall)}</dd>
          <dd className="metric-sub">
            <Meter value={rule.recall} label="Recall" />
            {rule.recall === null ? 'undefined: no labelled fraud' : 'of labelled fraud is caught'}
          </dd>
        </div>
      </dl>

      <dl className="facts">
        <div>
          <dt>Flagged</dt>
          <dd>
            {formatInteger(rule.flagged)} of {formatInteger(rule.total_transactions)} transactions
            {rule.flag_rate !== null && ` (${formatPercent(rule.flag_rate, 3)})`}
          </dd>
        </div>
        <div>
          <dt>Labelled</dt>
          <dd>
            {formatInteger(rule.flagged_labelled)} of the flagged have a fraud label;{' '}
            {formatInteger(rule.flagged_unlabelled)} {rule.flagged_unlabelled === 1 ? 'has' : 'have'} none
            and {rule.flagged_unlabelled === 1 ? 'is' : 'are'} unknown, not legitimate
          </dd>
        </div>
        <div>
          <dt>Lift</dt>
          <dd>
            {rule.lift === null || rule.base_fraud_rate === null
              ? 'undefined (no precision or no base fraud rate to compare)'
              : `${formatTimes(rule.lift)} times the base fraud rate (${formatPercent(rule.base_fraud_rate, 3)} of labelled transactions are fraud)`}
          </dd>
        </div>
        <div>
          <dt>Amounts</dt>
          <dd>
            {formatUsdCents(rule.flagged_amount_cents)} flagged;{' '}
            {formatUsdCents(rule.fraud_amount_caught_cents)} of it labelled fraud
          </dd>
        </div>
        {perCatch !== null && perCatch >= 1 && (
          <div>
            <dt>Cost</dt>
            <dd>
              About {formatInteger(Math.round(perCatch))} legitimate{' '}
              {Math.round(perCatch) === 1 ? 'transaction' : 'transactions'} flagged for each fraud caught
            </dd>
          </div>
        )}
      </dl>

      {rule.months.length > 0 && (
        <>
          <h3 className="rule-subhead">Month by month, on the same history</h3>
          <TableScroll className="months" label="Backtest by month">
            <table>
              <thead>
                <tr>
                  <th scope="col">Month</th>
                  <th scope="col" className="num">Flagged</th>
                  <th scope="col" className="num">Labelled</th>
                  <th scope="col" className="num">Fraud caught</th>
                  <th scope="col" className="num">False positives</th>
                  <th scope="col" className="num">Precision</th>
                </tr>
              </thead>
              <tbody>
                {rule.months.map((row) => {
                  const isWeakest = row.month === rule.weakest_month;
                  return (
                    <tr key={row.month} className={isWeakest ? 'weakest' : undefined}>
                      <th scope="row">
                        {row.month}
                        {isWeakest && <span className="badge badge-warn">Weakest</span>}
                      </th>
                      <td className="num">{formatInteger(row.flagged)}</td>
                      <td className="num">{formatInteger(row.flagged_labelled)}</td>
                      <td className="num">{formatInteger(row.true_positives)}</td>
                      <td className="num">{formatInteger(row.false_positives)}</td>
                      {row.precision === null ? (
                        <td className="num">–</td>
                      ) : (
                        <td className="num" title={rawTitle(String(row.precision))}>
                          {formatPercent(row.precision)}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableScroll>

          <dl className="facts">
            <div>
              <dt>Weakest month</dt>
              <dd>
                {weakest && weakest.precision !== null
                  ? `${weakest.month}: ${formatInteger(weakest.true_positives)} of ${formatInteger(weakest.flagged_labelled)} labelled flagged were fraud (${formatPercent(weakest.precision)})`
                  : `none: no month has ${stability.min_month_labelled} or more labelled flagged transactions`}
              </dd>
            </div>
            {stability.min_precision !== null && stability.max_precision !== null && (
              <div>
                <dt>Monthly precision</dt>
                <dd>
                  {formatPercent(stability.min_precision)} to {formatPercent(stability.max_precision)} over
                  the {stability.qualifying_months} {stability.qualifying_months === 1 ? 'month' : 'months'}{' '}
                  with {stability.min_month_labelled} or more labelled flagged, of {stability.months}
                </dd>
              </div>
            )}
            <Period name="Earlier period" period={rule.earlier} />
            <Period name="Later period" period={rule.later} />
          </dl>

          <footer className="rule-foot">
            <p>{EVIDENCE_NOTE}</p>
          </footer>
        </>
      )}
    </section>
  );
}
