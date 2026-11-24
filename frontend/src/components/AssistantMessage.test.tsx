import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Reply, TableResult, TurnResult } from '../api/types';
import { EVIDENCE_NOTE } from '../lib/evidence';
import { HEALTH, TURNS } from '../test/fixtures';
import { AssistantMessage } from './AssistantMessage';
import { ResultsTable } from './ResultsTable';

function show(reply: TurnResult | Reply | null, options: { isLatest?: boolean; memoryLost?: boolean } = {}) {
  const onRetry = vi.fn();
  const onSuggestion = vi.fn();
  const view = render(
    <AssistantMessage
      reply={reply === null || 'kind' in reply ? reply : { kind: 'turn', result: reply }}
      animate={false}
      isLatest={options.isLatest ?? true}
      busy={false}
      memoryLost={options.memoryLost ?? false}
      onRetry={onRetry}
      onSuggestion={onSuggestion}
    />,
  );
  return { ...view, onRetry, onSuggestion, user: userEvent.setup() };
}

const cells = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent);

describe('an answered question', () => {
  it('shows a single value with its SQL, the model text apart, and the footer', () => {
    show(TURNS.answeredScalar);
    expect(screen.getByText('transactions: 6')).toHaveClass('answer');
    expect(screen.getByRole('region', { name: 'SQL that ran' })).toHaveTextContent(
      'SELECT count(*) AS transactions FROM transactions',
    );
    expect(screen.getByRole('cell', { name: '6' })).toBeInTheDocument();

    const model = screen.getByRole('region', { name: 'Model text, not checked' });
    expect(model).toHaveTextContent('Written by the model and not checked against the data.');
    expect(model).toHaveTextContent('Counts every transaction.');
    expect(screen.getByLabelText('How this answer was produced')).toHaveTextContent(
      /tool: run_sql.*scripted.*1 model request.*1,000 in, 100 out tokens.*1\.2 s/,
    );
    expect(screen.queryByRole('list', { name: 'Caveats' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('follows the format hints: dollars, a fraction as a percentage, ungrouped ids, NULL', () => {
    show(TURNS.answeredTable);
    const rows = within(screen.getByRole('table')).getAllByRole('row');
    expect(within(rows[0]!).getAllByRole('columnheader').map((th) => th.textContent)).toEqual([
      'card_type', 'merchant_id', 'transactions', 'total_amount_usd_cents', 'fraud_rate',
    ]);
    expect(cells(rows[1]!)).toEqual(['Credit', '10', '2', '$67.00', '0.000%']);
    expect(cells(rows[2]!)).toEqual(['Debit', '10', '1', '−$50.00', '0.000%']);
    expect(cells(rows[3]!)).toEqual(['Debit', '20', '1', '$1,500.00', '100.000%']);
    // The value the query returned stays on the cell; a cell shown as it is has no tooltip.
    expect(within(rows[3]!).getAllByRole('cell').map((cell) => cell.getAttribute('title'))).toEqual([
      null, null, null, null, 'Value returned: 1',
    ]);
    expect(screen.getByText(/Fractions between 0 and 1 are shown as percentages/)).toBeInTheDocument();
    expect(cells(rows[5]!)).toEqual(['Debit (Prepaid)', '20', '1', '$1,200.00', 'NULL']);
    expect(screen.getByText('Amounts are stored in cents and shown in US dollars.')).toBeInTheDocument();
    expect(screen.getByText('5 rows')).toBeInTheDocument();

    const caveat = within(screen.getByRole('list', { name: 'Caveats' })).getByRole('listitem');
    expect(caveat).toHaveAttribute('data-code', 'label_coverage');
    expect(caveat).toHaveTextContent('An unlabelled transaction is unknown, not legitimate.');
    const assumptions = within(screen.getByRole('region', { name: 'Model text, not checked' })).getAllByRole('listitem');
    expect(assumptions.map((item) => item.textContent)).toEqual(TURNS.answeredTable.assumptions);
  });

  it('says when the result was capped, and when it is empty', () => {
    const { unmount } = show(TURNS.answeredTruncated);
    expect(screen.getByText('First 3 rows')).toBeInTheDocument();
    expect(screen.getByText(/Capped at the first 3 rows: more rows match than are shown\./)).toBeInTheDocument();
    expect(screen.getByText('truncated')).toHaveClass('caveat-code');
    expect(screen.getByRole('cell', { name: '1001' })).toBeInTheDocument();
    unmount();

    show(TURNS.answeredEmpty);
    expect(screen.getByText('The query ran and returned no rows.')).toBeInTheDocument();
    expect(screen.getByText('0 rows')).toBeInTheDocument();
    expect(screen.getByText('zero_rows')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('renders a long result in two steps', async () => {
    const table: TableResult = {
      columns: [{ name: 'id', format: 'identifier' }],
      rows: Array.from({ length: 60 }, (_, i) => [1000000 + i]),
      row_count: 60,
      truncated: false,
    };
    const user = userEvent.setup();
    render(<ResultsTable table={table} />);
    expect(screen.getAllByRole('row')).toHaveLength(26);
    expect(screen.getByRole('cell', { name: '1000000' })).toHaveClass('num');
    await user.click(screen.getByRole('button', { name: 'Show All 60 Rows' }));
    expect(screen.getAllByRole('row')).toHaveLength(61);
  });

  it('copies the table with the values the query returned, not the percentages', async () => {
    const user = userEvent.setup();
    render(<ResultsTable table={TURNS.scan.table!} />);
    await user.click(screen.getByRole('button', { name: 'Copy table with the values as returned' }));
    const lines = (await navigator.clipboard.readText()).split('\n');
    expect(lines[0]).toBe('dimension\tsegment\tlabelled\tfraud\tfraud_rate\tlift\tshare_of_fraud');
    expect(lines[1]).toBe(
      'merchant city and state\tRome, Italy\t200\t150\t0.75\t3.170634920634921\t0.7936507936507936',
    );
  });

  it('lets a long text cell wrap, with its text as a tooltip, and keeps a short one on a line', () => {
    const long = 'A merchant description that goes on for rather longer than a table column should have to hold';
    const table: TableResult = {
      columns: [{ name: 'description', format: 'plain' }, { name: 'card_type', format: 'plain' }],
      rows: [[long, 'Debit']],
      row_count: 1,
      truncated: false,
    };
    render(<ResultsTable table={table} />);
    expect(screen.getByText(long)).toHaveClass('cell-text', 'cell-text-long');
    expect(screen.getByText(long)).toHaveAttribute('title', long);
    expect(screen.getByText('Debit')).toHaveClass('cell-text');
    expect(screen.getByText('Debit')).not.toHaveClass('cell-text-long');
    expect(screen.getByText('Debit')).not.toHaveAttribute('title');
  });
});

describe('a table wider than its panel', () => {
  /** jsdom lays nothing out: give the scroll box the sizes a browser would report. */
  function resize(box: HTMLElement, sizes: { clientWidth: number; scrollWidth: number; scrollLeft: number }) {
    for (const [name, value] of Object.entries(sizes)) {
      Object.defineProperty(box, name, { configurable: true, value });
    }
    fireEvent.scroll(box);
  }

  it('scrolls inside a named, focusable box and says nothing while the table fits', () => {
    render(<ResultsTable table={TURNS.scan.table!} />);
    const box = screen.getByRole('region', { name: 'Results, 2 rows' });
    expect(box).toHaveClass('table-scroll');
    expect(box).toHaveAttribute('tabindex', '0');
    expect(box.parentElement).toHaveClass('table-wrap');
    expect(within(box).getByRole('table')).toBeInTheDocument();

    resize(box, { clientWidth: 700, scrollWidth: 700, scrollLeft: 0 });
    expect(box.parentElement).not.toHaveAttribute('data-more-right');
    expect(box.parentElement).not.toHaveAttribute('data-more-left');
    expect(screen.queryByText(/Scroll sideways for more columns/)).not.toBeInTheDocument();
  });

  it('marks the side with more columns and names the scroll', () => {
    render(<ResultsTable table={TURNS.scan.table!} />);
    const box = screen.getByRole('region', { name: 'Results, 2 rows' });
    const wrap = box.parentElement!;

    resize(box, { clientWidth: 320, scrollWidth: 700, scrollLeft: 0 });
    expect(wrap).toHaveAttribute('data-more-right');
    expect(wrap).not.toHaveAttribute('data-more-left');
    expect(screen.getByText(/Scroll sideways for more columns/)).toBeInTheDocument();

    resize(box, { clientWidth: 320, scrollWidth: 700, scrollLeft: 120 });
    expect(wrap).toHaveAttribute('data-more-right');
    expect(wrap).toHaveAttribute('data-more-left');

    resize(box, { clientWidth: 320, scrollWidth: 700, scrollLeft: 380 });
    expect(wrap).not.toHaveAttribute('data-more-right');
    expect(wrap).toHaveAttribute('data-more-left');
    expect(screen.getByText(/Scroll sideways for more columns/)).toBeInTheDocument();
  });

  it('gives the month table of a rule the same box', () => {
    show(TURNS.ruleProposed);
    const box = screen.getByRole('region', { name: 'Backtest by month' });
    expect(box.parentElement).toHaveClass('table-wrap', 'months');
    resize(box, { clientWidth: 300, scrollWidth: 620, scrollLeft: 0 });
    expect(box.parentElement).toHaveAttribute('data-more-right');
  });
});

describe('a proposed rule', () => {
  it('shows the clause, the overall backtest, the lift and the caveats', () => {
    show(TURNS.ruleProposed);
    expect(screen.getByText(/The rule flags 410 of 12,410 transactions\./)).toHaveClass('answer');

    const card = screen.getByRole('region', { name: 'Proposed rule' });
    expect(within(card).getByRole('region', { name: 'Rule WHERE clause' })).toHaveTextContent(
      "merchant_location_id IN (SELECT id FROM merchant_locations WHERE city = 'Rome' AND state = 'Italy')",
    );
    const entry = (term: string) => within(card).getByText(term, { selector: 'dt' }).closest('div') as HTMLElement;
    expect(entry('Fraud caught')).toHaveTextContent('160of 180 labelled fraud');
    expect(entry('False positives')).toHaveTextContent('210legitimate, worth $5,277.30');
    expect(entry('Precision')).toHaveTextContent('43.24%');
    expect(entry('Recall')).toHaveTextContent('88.89%');
    expect(within(card).getByRole('meter', { name: 'Recall' })).toHaveAttribute('aria-valuetext', '88.89%');
    expect(entry('Flagged')).toHaveTextContent('410 of 12,410 transactions (3.304%)');
    expect(entry('Labelled')).toHaveTextContent('370 of the flagged have a fraud label; 40 have none');
    expect(entry('Lift')).toHaveTextContent(
      '22.5 times the base fraud rate (1.921% of labelled transactions are fraud)',
    );
    expect(entry('Amounts')).toHaveTextContent('$10,303.30 flagged; $4,020.80 of it labelled fraud');

    const codes = within(screen.getByRole('list', { name: 'Caveats' }))
      .getAllByRole('listitem')
      .map((item) => item.getAttribute('data-code'));
    expect(codes).toEqual(['label_coverage', 'weak_month', 'period_drop']);
    expect(screen.queryByRole('region', { name: /SQL that ran/ })).not.toBeInTheDocument();
  });

  it('shows the month table with the weakest month marked, the two periods and the fixed sentence', () => {
    show(TURNS.ruleProposed);
    const months = screen.getByRole('region', { name: 'Backtest by month' });
    const rows = within(months).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(10);
    expect(rows[0]).toHaveTextContent('2019-01');
    expect(cells(rows[0]!)).toEqual(['150', '120', '90', '30', '75.00%']);
    expect(within(rows[0]!).getAllByRole('cell')[4]).toHaveAttribute('title', 'Value returned: 0.75');
    // A month the rule flags nothing in has no precision.
    expect(cells(rows[1]!)).toEqual(['0', '0', '0', '0', '–']);

    const weakest = rows.filter((row) => row.classList.contains('weakest'));
    expect(weakest).toHaveLength(1);
    expect(weakest[0]).toHaveTextContent('2019-05Weakest');
    expect(cells(weakest[0]!)).toEqual(['90', '80', '8', '72', '10.00%']);

    const card = screen.getByRole('region', { name: 'Proposed rule' });
    const entry = (term: string) => within(card).getByText(term, { selector: 'dt' }).closest('div') as HTMLElement;
    expect(entry('Weakest month')).toHaveTextContent('2019-05: 8 of 80 labelled flagged were fraud (10.00%)');
    expect(entry('Monthly precision')).toHaveTextContent(
      '10.00% to 75.00% over the 5 months with 30 or more labelled flagged, of 10',
    );
    const earlier = within(card).getByText(/Earlier period/).closest('div') as HTMLElement;
    expect(earlier).toHaveTextContent('2019-01 to 2019-07');
    expect(earlier).toHaveTextContent('55.00% precision (143 of 260 labelled flagged were fraud); recall 143 of 157; lift 23.0');
    const later = within(card).getByText(/Later period/).closest('div') as HTMLElement;
    expect(later).toHaveTextContent('2019-08 to 2019-10');
    expect(later).toHaveTextContent('15.45% precision (17 of 110 labelled flagged were fraud); recall 17 of 23; lift 18.9');

    expect(within(card).getByText(EVIDENCE_NOTE)).toBeInTheDocument();
    expect(EVIDENCE_NOTE).toContain('stability check, not a test on unseen data');
    expect(EVIDENCE_NOTE).toBe(HEALTH.live.evidence_note);
  });

  it('says so when no month has enough labelled transactions to compare', () => {
    const rule = TURNS.ruleProposed.rule!;
    show({
      ...TURNS.ruleProposed,
      rule: {
        ...rule,
        weakest_month: null,
        precision: null,
        lift: null,
        stability: { ...rule.stability, qualifying_months: 0, min_precision: null, max_precision: null },
        later: { ...rule.later, precision: null, lift: null },
      },
    });
    const card = screen.getByRole('region', { name: 'Proposed rule' });
    expect(card).toHaveTextContent('none: no month has 30 or more labelled flagged transactions');
    expect(card).toHaveTextContent('undefined: nothing flagged is labelled');
    expect(card).toHaveTextContent('undefined (no precision or no base fraud rate to compare)');
    expect(card).toHaveTextContent('Nothing flagged is labelled.');
    expect(card.querySelector('.weakest')).toBeNull();
  });
});

describe('the fraud scan', () => {
  it('shows its table and notes, keeps its long statement collapsed, and offers the next questions', async () => {
    const { user, onSuggestion } = show(TURNS.scan);
    expect(screen.getByText(/Labelled fraud is most concentrated in merchant city and state = Rome, Italy/)).toHaveClass('answer');

    const toggle = screen.getByRole('button', { name: /^SQL that ran \(the scan’s profile statement\)/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent(/\d\d lines/);
    expect(screen.queryByRole('region', { name: /SQL that ran/ })).not.toBeInTheDocument();
    await user.click(toggle);
    expect(screen.getByRole('region', { name: /SQL that ran/ })).toHaveTextContent('WITH labelled AS MATERIALIZED');

    const table = screen.getByRole('region', { name: /Segments where fraud is concentrated, 2 rows/ });
    const rows = within(table).getAllByRole('row');
    expect(cells(rows[1]!)).toEqual([
      'merchant city and state', 'Rome, Italy', '200', '150', '75.000%', '3.1706', '79.365%',
    ]);
    // A percentage carries the fraction the scan returned; lift has no hint and is shown as it is.
    expect(within(rows[1]!).getAllByRole('cell').map((cell) => cell.getAttribute('title'))).toEqual([
      null, null, null, null, 'Value returned: 0.75', null, 'Value returned: 0.7936507936507936',
    ]);

    const notes = within(screen.getByRole('region', { name: 'Notes' })).getAllByRole('listitem');
    expect(notes.map((note) => note.textContent)).toEqual(TURNS.scan.notes);
    expect(screen.queryByRole('region', { name: 'Model text, not checked' })).not.toBeInTheDocument();

    const chips = within(screen.getByRole('list', { name: 'Suggested questions' })).getAllByRole('button');
    expect(chips.map((chip) => chip.textContent)).toEqual(TURNS.scan.suggestions);
    await user.click(chips[0]!);
    expect(onSuggestion).toHaveBeenCalledWith(
      'Propose a rule for transactions with merchant city and state = Rome, Italy.',
    );
  });

  it('offers suggestions on the newest answer only', () => {
    show(TURNS.scan, { isLatest: false });
    for (const chip of within(screen.getByRole('list', { name: 'Suggested questions' })).getAllByRole('button')) {
      expect(chip).toBeDisabled();
    }
  });
});

describe('turns that did not answer', () => {
  it('asks a question back, with its suggestions', () => {
    show(TURNS.clarification);
    expect(screen.getByText('The assistant asks')).toBeInTheDocument();
    expect(screen.getByText('What should count as risky?')).toBeInTheDocument();
    expect(screen.getByText('Pick one, or answer in your own words')).toBeInTheDocument();
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Transactions labelled as fraud', 'Transactions above $1,000',
    ]);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows a decline as the model’s reason, without Retry', () => {
    show(TURNS.cannotAnswer);
    expect(screen.getByText('The assistant can’t answer this')).toBeInTheDocument();
    expect(screen.getByText('The data ends in October 2019 and holds no forecast.')).toBeInTheDocument();
    expect(screen.getByText('This reason was written by the model.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('How this answer was produced')).toHaveTextContent('tool: decline');
  });

  it('shows a guard rejection with its reason and codes, and no table', () => {
    show(TURNS.rejected);
    expect(screen.getByText('Blocked by the SQL safety guard')).toBeInTheDocument();
    expect(screen.getByText(/sqlite_master is not a table that may be read/)).toBeInTheDocument();
    expect(screen.getByText('Nothing was run against the database.')).toBeInTheDocument();
    expect(screen.getByLabelText('How this answer was produced')).toHaveTextContent(
      /refused: table_not_allowed, table_not_allowed.*2 model requests/,
    );
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText(/sqlite_master is not a table/, { selector: '.answer' })).not.toBeInTheDocument();
  });

  it('says in the footer how many write attempts the guard stopped, and only when there were any', () => {
    const footer = () => screen.getByLabelText('How this answer was produced');
    const first = show(TURNS.rejectedWrite);
    expect(screen.getByText('Blocked by the SQL safety guard')).toBeInTheDocument();
    expect(footer()).toHaveTextContent(/refused: [a-z_]+, [a-z_]+.*2 write attempts stopped by the guard/);
    first.unmount();

    const second = show(TURNS.answeredAfterWrite);
    expect(footer()).toHaveTextContent('1 write attempt stopped by the guard');
    expect(footer()).not.toHaveTextContent('write attempts');
    expect(screen.getByRole('table')).toBeInTheDocument();
    second.unmount();

    show(TURNS.rejected);
    expect(footer()).toHaveTextContent('refused: table_not_allowed');
    expect(footer()).not.toHaveTextContent('write attempt');
  });

  it('shows no write attempts for a result stored before the field existed', () => {
    const stored: Partial<TurnResult> = { ...TURNS.rejectedWrite };
    delete stored.write_attempts;
    show(stored as TurnResult);
    expect(screen.getByLabelText('How this answer was produced')).not.toHaveTextContent('write attempt');
  });

  it('shows a failed query with SQLite’s own words and Retry', async () => {
    const { user, onRetry } = show(TURNS.failedSql);
    expect(screen.getByText('The query failed to run')).toBeInTheDocument();
    expect(screen.getByText('The query passed the checks but failed while it was running.')).toBeInTheDocument();
    expect(screen.getByText("bad JSON path: '1 Elm Street'")).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('shows a model failure, and offers Retry on the newest message only', () => {
    const { unmount } = show(TURNS.failedModel);
    expect(screen.getByText('The model didn’t respond')).toBeInTheDocument();
    expect(screen.getByText(/No reply from the model \(timeout\)/)).toBeInTheDocument();
    expect(screen.getByLabelText('How this answer was produced')).toHaveTextContent('tool: none');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    unmount();
    show(TURNS.failedModel, { isLatest: false });
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it.each([
    ['network', 'Can’t reach the server', true],
    ['busy', 'Still answering the previous question', true],
    ['server', 'The server failed', true],
    ['unreadable', 'The reply could not be read', true],
    ['invalid', 'The message was not accepted', false],
  ] as const)('shows a request that got no turn back (%s)', (reason, title, retry) => {
    show({ kind: 'failure', failure: { reason, message: 'What the server said.' } });
    expect(screen.getByText(title)).toBeInTheDocument();
    expect(screen.getByText('What the server said.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' }) !== null).toBe(retry);
    expect(screen.queryByLabelText('How this answer was produced')).not.toBeInTheDocument();
  });

  it('shows a question that got no reply, and a turn answered without its context', () => {
    const { unmount } = show(null);
    expect(screen.getByText('No reply arrived for this question.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    unmount();
    show(TURNS.answeredScalar, { memoryLost: true });
    expect(screen.getByText(/The server was restarted since the earlier questions/)).toBeInTheDocument();
  });

  it('labels an answer of the offline demo', () => {
    const demo: TurnResult = {
      ...TURNS.answeredScalar,
      meta: { ...TURNS.answeredScalar.meta, model_id: HEALTH.demo.model_id },
    };
    show(demo);
    expect(screen.getByText('Offline demo')).toHaveClass('badge');
  });
});

describe('text that code did not write', () => {
  it('is never rendered as HTML, and shows what would be hidden', () => {
    const hostile: TurnResult = {
      ...TURNS.answeredHostile,
      answer: '<img src=x onerror=alert(1)> answer',
      suggestions: ['<script>alert(1)</script>', 'plain\u202etext'],
      notes: ['<iframe src="javascript:alert(1)"></iframe>'],
      caveats: [{ code: 'truncated', message: '<svg onload=alert(1)>' }],
      where_clause: null,
    };
    const { container } = show(hostile);

    expect(container.querySelector('img, script, iframe, svg[onload], b, i')).toBeNull();
    expect(container.innerHTML).not.toContain('<img');
    expect(container.innerHTML).not.toContain('<script');
    expect(container.innerHTML).not.toContain('\u001b');
    expect(container.innerHTML).not.toContain('\u0000');
    expect(container.innerHTML).not.toContain('\u202e');

    expect(screen.getByText('<img src=x onerror=alert(1)> answer')).toHaveClass('answer');
    expect(screen.getByRole('cell', { name: '<img src=x onerror=alert(1)>' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'first\\nsecond\\x1b[31m\\x00end' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '<b>bold</b>' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '<i>name</i>' })).toBeInTheDocument();
    const model = screen.getByRole('region', { name: 'Model text, not checked' });
    expect(model).toHaveTextContent("<img src=x onerror=alert('explanation')> with a tab and an \\x1b[31mescape.");
    expect(model).toHaveTextContent("<script>alert('assumption')</script>");
    expect(screen.getByRole('button', { name: '<script>alert(1)</script>' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'plain\\u202etext' })).toBeInTheDocument();
    expect(screen.getByText('<svg onload=alert(1)>')).toBeInTheDocument();
    expect(screen.getByText('<iframe src="javascript:alert(1)"></iframe>')).toBeInTheDocument();
  });

  it('sends a suggestion exactly as it was given', async () => {
    const { user, onSuggestion } = show({ ...TURNS.clarification, suggestions: ['plain\u202etext', 'other'] });
    await user.click(screen.getByRole('button', { name: 'plain\\u202etext' }));
    expect(onSuggestion).toHaveBeenCalledWith('plain\u202etext');
  });
});
