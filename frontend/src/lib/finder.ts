import type { BankEntry, Caveat, Decision, EvidenceStatus, StepName } from '../api/types';

/** The words for a status and a decision, and the badge each wears. */
export const STATUS_LABEL: Record<EvidenceStatus, string> = {
  confirmed: 'Confirmed',
  not_confirmed: 'Not confirmed',
  no_signal: 'No signal',
  not_built: 'No rule built',
  proposed: 'Proposed',
  discarded: 'Discarded',
};

export const STATUS_BADGE: Record<EvidenceStatus, string> = {
  confirmed: 'badge-ok',
  not_confirmed: 'badge-warn',
  no_signal: 'badge-muted',
  not_built: 'badge-muted',
  proposed: 'badge-muted',
  discarded: 'badge-muted',
};

export const STATUS_MEANING: Record<EvidenceStatus, string> = {
  confirmed: 'The rule met every check in the discovery period and again in the confirmation period.',
  not_confirmed: 'The rule met the checks in the discovery period and did not hold in the confirmation period.',
  no_signal: 'The rule did not meet the checks in the discovery period.',
  not_built: 'The scenario did not become a rule, so there is nothing to measure.',
  proposed: 'A scenario waiting to be processed. No rule has been written for it and nothing has been run.',
  discarded: 'A scenario a reviewer set aside. It is kept so that it is not proposed again.',
};

export const DECISION_LABEL: Record<Decision, string> = {
  pending: 'Pending',
  approved: 'Approved',
  rejected: 'Rejected',
};

export const DECISION_BADGE: Record<Decision, string> = {
  pending: 'badge-muted',
  approved: 'badge-ok',
  rejected: 'badge-danger',
};

/** How the turn of a scenario that built no rule ended. */
export const OUTCOME_LABEL: Record<NonNullable<BankEntry['turn_outcome']>, string> = {
  answered: 'The model answered with a query, not a rule',
  rule_proposed: 'A rule was proposed',
  clarification: 'The model asked what the scenario meant',
  cannot_answer: 'The model declined',
  rejected: 'The SQL guard refused the clause, twice',
  failed: 'The turn failed',
};

/** `2000-01-01T00:00:00+00:00` → `2000-01-01 00:00 UTC`. */
export function formatStamp(stamp: string): string {
  return `${stamp.slice(0, 10)} ${stamp.slice(11, 16)} UTC`;
}

/** The steps of processing one scenario, in order, as the page names them. */
export const STEPS: { name: StepName; label: string }[] = [
  { name: 'write_rule', label: 'Writing the rule (model request)' },
  { name: 'check_rule', label: 'Checking the rule (SQL guard)' },
  { name: 'backtest', label: 'Testing on January to July, then confirming on August to October' },
  { name: 'duplicates', label: 'Checking the bank for the same rule' },
  { name: 'verdict', label: 'Judging the evidence' },
];

/** What every delete confirmation says: a delete forgets, a discard does not. */
export const DELETE_WARNING =
  'This permanently removes the scenario, its rule, the evidence, the review decision and its note. ' +
  'A deleted scenario is forgotten: the finder may propose it again, and the duplicate check will no ' +
  'longer catch it. Discarding, by contrast, keeps a scenario out.';

/** What a caveat is about, in words; its code stays beside it as a small label. */
export const CAVEAT_TITLE: Record<Caveat['code'], string> = {
  truncated: 'The result was cut at the row cap',
  label_coverage: 'Not every flagged transaction has a fraud label',
  zero_rows: 'The rule flags no transactions',
  small_sample: 'Too few labelled transactions to rely on',
  high_flag_rate: 'The rule flags a large share of all transactions',
  weak_month: 'Precision is much lower in some months',
  period_drop: 'The rule did worse in the later months',
  single_segment: 'All the fraud sits in one segment',
};

/** The caveats worth showing: one about label coverage says nothing when nothing is flagged. */
export function shownCaveats(entry: BankEntry): Caveat[] {
  const nothing = entry.rule?.flagged === 0;
  return entry.caveats.filter((caveat) => !(nothing && caveat.code === 'label_coverage'));
}

/**
 * A rule clause laid out to be read: a new line before each AND and OR that
 * is outside every pair of parentheses and every quoted text. Nothing else is
 * changed, and the clause that is copied is the original.
 */
export function formatClause(clause: string): string {
  let out = '';
  let depth = 0;
  let quote: string | null = null;
  for (let i = 0; i < clause.length; i += 1) {
    const character = clause[i]!;
    if (quote !== null) {
      if (character === quote) quote = null;
    } else if (character === "'" || character === '"' || character === '`') {
      quote = character;
    } else if (character === '(') {
      depth += 1;
    } else if (character === ')') {
      depth = Math.max(0, depth - 1);
    } else if (depth === 0 && /\s/.test(character)) {
      const word = /^\s+(AND|OR)\s/i.exec(clause.slice(i));
      if (word) {
        out += `\n${word[1]!.toUpperCase()} `;
        i += word[0].length - 1;
        continue;
      }
    }
    out += character;
  }
  return out.trim();
}
