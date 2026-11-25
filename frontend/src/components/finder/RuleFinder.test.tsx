import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../App';
import { createHttpClient } from '../../api/httpClient';
import { API_ERROR_FINDER_BUSY, FINDER, HEALTH } from '../../test/fixtures';
import { createMockClient, type MockOptions } from '../../test/mockClient';
import { formatClause } from '../../lib/finder';
import { RuleFinder } from './RuleFinder';

function setup(finder: MockOptions['finder'] = {}, demo = false) {
  const client = createMockClient({ finder });
  const user = userEvent.setup();
  render(<RuleFinder client={client} demo={demo} pollMs={5} />);
  return { client, user };
}

async function bank() {
  return within(await screen.findByRole('region', { name: 'Ranked rules' }));
}

async function open(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(await screen.findByRole('button', { name: title }));
}

describe('the rule bank', () => {
  it('lists the entries ranked, with the discovery figures and the confirmation result', async () => {
    setup();
    const table = await bank();
    const rows = table.getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getByRole('rowheader').textContent)).toEqual(
      FINDER.overview.entries.map((entry) => entry.title),
    );
    const rome = within(rows[0]!);
    expect(rome.getByText('Confirmed')).toBeInTheDocument();
    expect(rome.getByText('Pending')).toBeInTheDocument();
    expect(rome.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'Confirmedlift 7.4, then 10.0', 'Pending', '220', '60', '140', '30.00%', '61.86%', '7.4', 'lift 10.0; 30 of 100',
    ]);
    expect(within(rows[1]!).getByText('Not confirmed')).toBeInTheDocument();
    expect(within(rows[1]!).getByText('0 fraud caught on confirmation')).toBeInTheDocument();
    expect(within(rows[4]!).getByText('refused by the guard')).toBeInTheDocument();
    expect(within(rows[2]!).getByText('No signal')).toBeInTheDocument();
    expect(within(rows[3]!).getByText('No rule to measure')).toBeInTheDocument();
    expect(screen.getByText(/5 entries: 1 confirmed, 0 approved/)).toBeInTheDocument();
    expect(screen.getByText(FINDER.overview.note)).toBeInTheDocument();
    expect(screen.getByText(/1 scenario discarded/)).toBeInTheDocument();
  });

  it('says so when the bank is empty, and still offers a run', async () => {
    setup({ overview: FINDER.overviewEmpty });
    expect(await screen.findByText(/holds no processed scenario yet/)).toBeInTheDocument();
    expect(screen.getByText(/No scenario is waiting/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Find a rule' })).toBeEnabled();
    expect(screen.getByText(/One click asks for one scenario: 1 model request/)).toBeInTheDocument();
  });

  it('says that no rule is confirmed when none is, and still shows how far each got', async () => {
    setup({ overview: FINDER.overviewNoneConfirmed });
    expect(await screen.findByRole('note')).toHaveTextContent('No rule in the bank is confirmed.');
    const table = await bank();
    expect(table.getAllByRole('row')).toHaveLength(1 + FINDER.overviewNoneConfirmed.entries.length);
    expect(table.queryByText('Confirmed')).not.toBeInTheDocument();
  });
});

describe('an entry opened', () => {
  it('says first what happened and what rule was tested, then the scenario, the assumptions and the checks', async () => {
    const { user } = setup();
    const entry = FINDER.entries.confirmed;
    await open(user, entry.title);
    const panel = await screen.findByRole('region', { name: `Evidence: ${entry.title}` });
    await within(panel).findByRole('region', { name: 'Verdict' });

    // The order a reviewer reads in.
    const order = ['Verdict', 'The rule that was tested', 'Scenario', 'Assumptions', 'Checks'].map((name) =>
      within(panel).getByRole('region', { name }),
    );
    for (let i = 1; i < order.length; i += 1) {
      expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(order[0]).toHaveTextContent(entry.verdict);
    expect(order[0]).toHaveTextContent('Confirmed');
    const rule = within(order[1]!);
    expect(rule.getByText('The rule: the WHERE clause that was tested')).toBeInTheDocument();
    expect(rule.getByRole('region', { name: "The rule's WHERE clause" })).toHaveTextContent('merchant_location_id IN');
    expect(rule.getByRole('button', { name: 'Copy the rule' })).toBeInTheDocument();
    expect(within(order[2]!).getByText(entry.hypothesis, { exact: false })).toBeInTheDocument();
    for (const assumed of [...entry.scenario_assumptions, ...entry.rule_assumptions]) {
      expect(within(order[3]!).getByText(assumed)).toBeInTheDocument();
    }
    expect(within(order[4]!).getAllByText('Met')).toHaveLength(entry.criteria.length);
    expect(within(order[4]!).getByText(/At least 5 frauds caught: 60\./)).toBeInTheDocument();

    // The evidence is beside the table, not inside the box that scrolls sideways.
    expect(panel.closest('.table-scroll')).toBeNull();
    expect(screen.getByRole('region', { name: 'Ranked rules' })).not.toContainElement(panel);
  });

  it('keeps the tables and the SQL behind it closed until asked for', async () => {
    const { user } = setup();
    const entry = FINDER.entries.confirmed;
    await open(user, entry.title);
    const details = (await screen.findByText('Backtest figures, month by month and by period')).closest('details')!;
    expect(details).not.toHaveAttribute('open');
    const card = within(details);
    expect(card.getByRole('region', { name: 'Backtest by month' })).toBeInTheDocument();
    expect(card.getByText(/^Discovery period/)).toBeInTheDocument();
    expect(card.getByText(/^Confirmation period/)).toBeInTheDocument();
    expect(card.queryByText(/Earlier period/)).not.toBeInTheDocument();
    expect(card.getByText(FINDER.overview.note)).toBeInTheDocument();
    expect(card.queryByText(/same history it was written from/)).not.toBeInTheDocument();
    for (const title of [/^Backtest SQL, whole history/, /^Backtest SQL, by month/]) {
      expect(screen.getByRole('button', { name: title })).toHaveAttribute('aria-expanded', 'false');
    }
    await user.click(screen.getByRole('button', { name: /^Backtest SQL, whole history/ }));
    expect(screen.getByRole('button', { name: /^Backtest SQL, whole history/ })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/led to a clause that flags exactly the same transactions/)).toBeInTheDocument();
    expect(screen.getByText(/Entry 1, from run 1/)).toHaveTextContent('scenarios-2 and turn-5');
  });

  it('names each caveat in words and keeps its code as a small label', async () => {
    const { user } = setup();
    await open(user, FINDER.entries.confirmed.title);
    const caveats = within(await screen.findByRole('list', { name: 'Caveats' }));
    expect(caveats.getAllByRole('listitem')).toHaveLength(FINDER.entries.confirmed.caveats.length);
    expect(caveats.getByText('Not every flagged transaction has a fraud label')).toBeInTheDocument();
    expect(caveats.getByText('label_coverage')).toHaveClass('caveat-code');
    expect(caveats.getByText('The rule flags a large share of all transactions')).toBeInTheDocument();
  });

  it('says plainly that a rule flagged nothing, and why, without tables of zeros read as findings', async () => {
    const entry = FINDER.entries.flagsNothing;
    const { user } = setup({ overview: FINDER.overviewNoneConfirmed, entries: [entry] });
    await open(user, entry.title);
    const verdict = await screen.findByRole('region', { name: 'Verdict' });
    expect(verdict).toHaveTextContent('No signal: this rule flagged 0 of 3,520 transactions, so there was nothing to measure.');
    expect(verdict).toHaveTextContent('The likely reason: no row of cards has card_on_dark_web = 1.');
    // The long one-line clause is laid out at its top-level ANDs, and copied as it was written.
    const clause = screen.getByRole('region', { name: "The rule's WHERE clause" });
    expect(entry.clause).not.toContain('\n');
    expect(clause.textContent!.split('\n')).toHaveLength(4);
    expect(clause.textContent).toContain('\nAND transactions.card_id IN (SELECT c.id FROM cards c WHERE c.card_on_dark_web = 1)\n');
    expect(clause).toHaveClass('sql-wrap');
    // No "0 of the 0 flagged transactions have a fraud label".
    const caveats = within(screen.getByRole('list', { name: 'Caveats' }));
    expect(caveats.getAllByRole('listitem')).toHaveLength(1);
    expect(caveats.getByText('The rule flags no transactions')).toBeInTheDocument();
    expect(screen.queryByText(/0 of the 0 flagged/)).not.toBeInTheDocument();
    const row = within(screen.getByRole('region', { name: 'Ranked rules' }));
    expect(row.getByText('flagged nothing')).toBeInTheDocument();
  });

  it('names the checks a rule did not meet', async () => {
    const { user } = setup();
    await open(user, FINDER.entries.notConfirmed.title);
    expect(await screen.findByRole('region', { name: 'Verdict' })).toHaveTextContent(/^Not confirmed What happened/);
    const checks = within(screen.getByRole('region', { name: 'Checks' }));
    expect(checks.getAllByText('Not met')).toHaveLength(
      FINDER.entries.notConfirmed.criteria.filter((check) => !check.passed).length,
    );
  });

  it('says why no rule was built, and offers no decision', async () => {
    const { user } = setup();
    await open(user, FINDER.entries.clarification.title);
    const verdict = await screen.findByRole('region', { name: 'Verdict' });
    expect(verdict).toHaveTextContent('No rule built: the model asked what the scenario meant instead of writing a rule.');
    expect(verdict).toHaveTextContent(FINDER.entries.clarification.turn_message!);
    expect(screen.getByRole('region', { name: 'Assumptions' })).toHaveTextContent('Larger than usual is left undefined.');
    expect(screen.queryByRole('region', { name: 'The rule that was tested' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Checks' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();

    await open(user, FINDER.entries.rejected.title);
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Verdict' })).toHaveTextContent('the SQL guard refused the clause'),
    );
    expect(screen.getByRole('region', { name: 'Verdict' })).toHaveTextContent('label_read, label_read');
    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('region', { name: 'Verdict' })).not.toBeInTheDocument();
  });

  it('renders model text as text, never as markup', async () => {
    const hostile = {
      ...FINDER.entries.confirmed,
      hypothesis: '<img src=x onerror=alert(1)> hypothesis',
      scenario_assumptions: ['<script>alert(2)</script>'],
    };
    const { user } = setup({ entries: [hostile] });
    await open(user, hostile.title);
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)> hypothesis/)).toBeInTheDocument();
    expect(screen.getByText('<script>alert(2)</script>')).toBeInTheDocument();
    expect(document.querySelector('img, script')).toBeNull();
  });
});

describe('a review decision', () => {
  it('is sent with its note and shown on the entry and in the table', async () => {
    const { client, user } = setup();
    await open(user, FINDER.entries.confirmed.title);
    await user.type(await screen.findByLabelText('Note (optional)'), 'Holds for Rome.');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    // The form is rebuilt from the saved entry after each decision.
    const review = () => within(screen.getByRole('region', { name: 'Review decision' }));
    await waitFor(() => expect(review().getByRole('status')).toHaveTextContent('Approved'));
    expect(review().getByRole('status')).toHaveTextContent('Holds for Rome.');
    expect(review().getByLabelText('Note (optional)')).toHaveValue('Holds for Rome.');
    expect(client.decisions).toEqual([{ id: 1, decision: 'approved', note: 'Holds for Rome.' }]);
    const row = within((await bank()).getAllByRole('row')[1]!);
    expect(row.getByText('Approved')).toBeInTheDocument();
    expect(screen.getByText(/1 confirmed, 1 approved/)).toBeInTheDocument();

    await user.click(review().getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(review().getByRole('status')).toHaveTextContent('Rejected'));
    await user.click(review().getByRole('button', { name: 'Back to pending' }));
    await waitFor(() => expect(review().getByRole('status')).toHaveTextContent('Pending'));
    expect(client.decisions.map((sent) => sent.decision)).toEqual(['approved', 'rejected', 'pending']);
  });
});

const WAITING = FINDER.overviewWaiting.proposed;

/** The steps list as the page shows it: each step's state, in order. */
function stepStates() {
  const panel = within(screen.getByRole('region', { name: 'Processing a scenario' }));
  return within(panel.getByRole('list'))
    .getAllByRole('listitem')
    .map((item) => item.dataset.state);
}

describe('find a rule', () => {
  it('asks for one scenario and shows it waiting, with nothing processed', async () => {
    const { client, user } = setup({
      overview: FINDER.overviewEmpty,
      afterPropose: { ...FINDER.overviewWaiting, proposed: WAITING.slice(0, 1) },
    });
    await user.click(await screen.findByRole('button', { name: 'Find a rule' }));
    const card = within(await screen.findByRole('listitem', { name: `Proposed scenario: ${WAITING[0]!.title}` }));
    expect(card.getByText(WAITING[0]!.hypothesis, { exact: false })).toBeInTheDocument();
    for (const assumed of WAITING[0]!.assumptions) expect(card.getByText(assumed)).toBeInTheDocument();
    expect(card.getByText(/merchant_locations\.city/)).toBeInTheDocument();
    expect(card.getByText(/Process calls the model: it writes the rule \(2 requests at most\)/)).toBeInTheDocument();
    expect(card.getByRole('button', { name: /^Process scenario/ })).toBeEnabled();
    expect(client.calls).toEqual(['propose']);
    expect(screen.queryByRole('region', { name: 'Processing a scenario' })).not.toBeInTheDocument();
  });

  it('shows why no scenario came back', async () => {
    const { user } = setup({ propose: FINDER.proposedFailed });
    await user.click(await screen.findByRole('button', { name: 'Find a rule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(FINDER.proposedFailed.error!.message);
  });

  it('says plainly when the offline demo has no canned scenario left', async () => {
    const { user } = setup({ propose: FINDER.proposedDemoExhausted }, true);
    expect(await screen.findByText(/Offline demo: the scenarios and clauses are canned/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Find a rule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The offline demo has no canned scenario left');
  });

  it('shows the server’s sentence when the finder is busy', async () => {
    const { user } = setup({ propose: { reason: 'busy', message: API_ERROR_FINDER_BUSY.message } });
    await user.click(await screen.findByRole('button', { name: 'Find a rule' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The rule finder is busy with another request.');
  });
});

describe('processing a scenario', () => {
  it('shows each step as it happens, then the result, and opens the rule', async () => {
    let release = () => {};
    const hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { client, user } = setup({
      overview: FINDER.overviewWaiting,
      frames: FINDER.progress.confirmed,
      afterProcess: FINDER.overview,
      hold,
    });
    await user.click(await screen.findByRole('button', { name: `Process scenario: ${WAITING[0]!.title}` }));

    const panel = within(await screen.findByRole('region', { name: 'Processing a scenario' }));
    expect(panel.getByRole('heading')).toHaveTextContent(`Processing: “${WAITING[0]!.title}”`);
    expect(stepStates()).toEqual(['current', 'pending', 'pending', 'pending', 'pending']);
    expect(panel.getByText('Writing the rule (model request)').closest('li')).toHaveAttribute('aria-current', 'step');
    expect(panel.getByRole('status')).toHaveTextContent('Writing the rule (model request)…');
    // While it runs: its own card says so, and no other action can start.
    expect(screen.getByText('Being processed: see the steps above.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: `Process scenario: ${WAITING[1]!.title}` })).toBeDisabled();
    expect(screen.getByRole('button', { name: `Discard scenario: ${WAITING[1]!.title}` })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Find a rule' })).toBeDisabled();

    release();
    await waitFor(() => expect(panel.getByRole('heading')).toHaveTextContent('Processed:'));
    expect(stepStates()).toEqual(['done', 'done', 'done', 'done', 'done']);
    expect(panel.getByLabelText('Rule WHERE clause')).toHaveTextContent('merchant_location_id IN');
    expect(panel.getByRole('status')).toHaveTextContent(
      `${FINDER.entries.confirmed.verdict} The rule and its evidence are open below.`,
    );
    expect(client.calls.filter((call) => call.startsWith('progress')).length).toBeGreaterThan(3);

    // The result opens by itself in the table.
    expect(await screen.findByRole('region', { name: 'Verdict' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: FINDER.entries.confirmed.title })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: 'Find a rule' })).toBeEnabled();
    await user.click(panel.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByRole('region', { name: 'Processing a scenario' })).not.toBeInTheDocument();
  });

  it('says which rule a duplicate matched, and opens that rule', async () => {
    const { user } = setup({
      overview: FINDER.overviewWaiting,
      frames: FINDER.progress.duplicate,
      afterProcess: FINDER.overview,
    });
    await user.click(await screen.findByRole('button', { name: `Process scenario: ${WAITING[5]!.title}` }));
    const panel = within(await screen.findByRole('region', { name: 'Processing a scenario' }));
    await waitFor(() =>
      expect(panel.getByRole('status')).toHaveTextContent(
        'Duplicate: the model wrote a clause that flags exactly the same transactions as rule 1, so no entry was added. Rule 1 is open below.',
      ),
    );
    expect(stepStates()).toEqual(['done', 'done', 'done', 'done', 'unreached']);
    expect(await screen.findByRole('button', { name: FINDER.entries.confirmed.title })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it('shows a second attempt, and a scenario that became no rule', async () => {
    const { user } = setup({
      overview: FINDER.overviewWaiting,
      frames: FINDER.progress.rejected,
      afterProcess: FINDER.overview,
    });
    await user.click(await screen.findByRole('button', { name: `Process scenario: ${WAITING[4]!.title}` }));
    const panel = within(await screen.findByRole('region', { name: 'Processing a scenario' }));
    await waitFor(() =>
      expect(panel.getByRole('status')).toHaveTextContent(
        'No rule built: the SQL guard refused the clause the model wrote, and the one it wrote after that. The entry is open below.',
      ),
    );
    expect(panel.getByText(/second attempt, after the first was refused or failed/)).toBeInTheDocument();
    expect(stepStates()).toEqual(['done', 'unreached', 'unreached', 'unreached', 'unreached']);
  });

  it('shows a failure mid-run and leaves the scenario waiting', async () => {
    const { user } = setup({ overview: FINDER.overviewWaiting, frames: FINDER.progress.failed });
    await user.click(await screen.findByRole('button', { name: `Process scenario: ${WAITING[0]!.title}` }));
    const panel = within(await screen.findByRole('region', { name: 'Processing a scenario' }));
    await waitFor(() => expect(panel.getByRole('heading')).toHaveTextContent('Processing stopped'));
    expect(panel.getByRole('status')).toHaveTextContent('The model did not answer, so no rule was written.');
    expect(screen.getByRole('button', { name: `Process scenario: ${WAITING[0]!.title}` })).toBeEnabled();
  });

  it('shows the refusal when a run is already in progress', async () => {
    const { user } = setup({
      overview: FINDER.overviewWaiting,
      process: { reason: 'busy', message: API_ERROR_FINDER_BUSY.message },
    });
    await user.click(await screen.findByRole('button', { name: `Process scenario: ${WAITING[0]!.title}` }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The scenario was not processed. The rule finder is busy');
  });

  it('picks a run back up when the page is loaded in the middle of it', async () => {
    const frames = FINDER.progress.confirmed;
    setup({
      overview: { ...FINDER.overviewWaiting, running: true, active: frames[2]! },
      frames: frames.slice(2),
      afterProcess: FINDER.overview,
    });
    const panel = within(await screen.findByRole('region', { name: 'Processing a scenario' }));
    expect(panel.getByRole('heading')).toHaveTextContent('Processing:');
    await waitFor(() => expect(panel.getByRole('heading')).toHaveTextContent('Processed:'));
    expect(await screen.findByRole('region', { name: 'Verdict' })).toBeInTheDocument();
  });

  it('says so when the last run was interrupted by a restart', async () => {
    const last = { ...FINDER.overview.last_run!, status: 'interrupted' as const };
    setup({ overview: { ...FINDER.overviewWaiting, last_run: last } });
    expect(await screen.findByText(/was interrupted: the server stopped before it ended/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: `Process scenario: ${WAITING[0]!.title}` })).toBeEnabled();
  });
});

describe('discard and delete', () => {
  it('discards a scenario without asking, and it leaves the list', async () => {
    const { client, user } = setup({ overview: FINDER.overviewWaiting });
    await user.click(await screen.findByRole('button', { name: `Discard scenario: ${WAITING[7]!.title}` }));
    await waitFor(() =>
      expect(screen.queryByRole('listitem', { name: `Proposed scenario: ${WAITING[7]!.title}` })).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/1 scenario discarded\. They are kept/)).toBeInTheDocument();
    expect(client.calls).toEqual(['discard 8']);
  });

  it('deletes only after an in-page confirmation that says what a delete forgets', async () => {
    const { client, user } = setup({ overview: FINDER.overviewWaiting });
    const subject = `scenario: ${WAITING[0]!.title}`;
    await user.click(await screen.findByRole('button', { name: `Delete ${subject}` }));
    const confirm = within(screen.getByRole('group', { name: `Confirm deleting ${subject}` }));
    expect(confirm.getByText(/the finder may propose it again, and the duplicate check will no longer catch it/)).toBeInTheDocument();
    expect(confirm.getByText(/Discarding, by contrast, keeps a scenario out/)).toBeInTheDocument();

    await user.click(confirm.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('group', { name: `Confirm deleting ${subject}` })).not.toBeInTheDocument();
    expect(client.calls).toEqual([]);

    await user.click(screen.getByRole('button', { name: `Delete ${subject}` }));
    await user.click(screen.getByRole('button', { name: 'Delete for good' }));
    await waitFor(() =>
      expect(screen.queryByRole('listitem', { name: `Proposed scenario: ${WAITING[0]!.title}` })).not.toBeInTheDocument(),
    );
    expect(client.calls).toEqual(['delete 1']);
  });

  it('deletes a processed rule from its opened entry, whatever its decision', async () => {
    const { client, user } = setup();
    await open(user, FINDER.entries.confirmed.title);
    await user.click(await screen.findByRole('button', { name: 'Delete entry 1' }));
    await user.click(screen.getByRole('button', { name: 'Delete for good' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: FINDER.entries.confirmed.title })).not.toBeInTheDocument(),
    );
    expect(client.calls).toEqual(['delete 1']);
    expect(screen.getByText(/4 entries: 0 confirmed/)).toBeInTheDocument();
  });
});

describe('the two views', () => {
  function app() {
    const user = userEvent.setup();
    const client = createMockClient({ health: HEALTH.demo });
    render(<App client={client} />);
    const views = within(screen.getByRole('navigation', { name: 'Views' }));
    return { user, client, views };
  }

  it('shows nothing of the chat in the finder view, and nothing of the finder in the chat', async () => {
    const { user, views } = app();
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'How many transactions are there?{Enter}');
    expect(await screen.findAllByText(/transactions: 6/)).not.toHaveLength(0);
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'a draft');
    expect(views.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('heading', { name: 'Rule finder' })).not.toBeInTheDocument();

    await user.click(views.getByRole('button', { name: 'Rule finder' }));
    expect(await screen.findByRole('heading', { name: 'Rule finder' })).toBeInTheDocument();
    expect(views.getByRole('button', { name: 'Rule finder' })).toHaveAttribute('aria-current', 'page');
    expect(views.getByRole('button', { name: 'Chat' })).not.toHaveAttribute('aria-current');
    // Not accessible: no composer, no send button, no transcript, no conversations, no new chat.
    expect(screen.queryByRole('textbox', { name: 'Message' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /send/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /message box/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /new chat/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /conversations/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Conversations' })).not.toBeInTheDocument();
    expect(screen.queryByText('How many transactions are there?')).not.toBeVisible();
    // Hidden and inert, not merely covered.
    const composer = document.getElementById('composer-input')!;
    expect(composer.closest('[hidden][inert]')).not.toBeNull();
    expect(document.querySelector('.thread')?.closest('[hidden][inert]')).not.toBeNull();
    expect(document.getElementById('sidebar')).toBeNull();

    await user.click(views.getByRole('button', { name: 'Chat' }));
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('a draft');
    expect(screen.getAllByText('How many transactions are there?')[0]).toBeVisible();
    expect(screen.getByRole('navigation', { name: 'Conversations' })).toBeInTheDocument();
    expect(composer.closest('[hidden], [inert]')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Rule finder' })).not.toBeInTheDocument();
  });

  it('keeps keyboard focus out of the chat while the finder is shown', async () => {
    const { user, views } = app();
    await user.click(views.getByRole('button', { name: 'Rule finder' }));
    await screen.findByRole('button', { name: 'Find a rule' });
    const reached = new Set<Element>();
    for (let i = 0; i < 40; i += 1) {
      await user.tab();
      if (document.activeElement) reached.add(document.activeElement);
    }
    expect(reached.size).toBeGreaterThan(3);
    for (const element of reached) {
      expect(element.closest('.dock, #sidebar, [hidden]')).toBeNull();
      expect(element.id).not.toBe('composer-input');
    }
  });
});

describe('the HTTP client', () => {
  const respond = (status: number, body: unknown) =>
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(body === null ? null : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  afterEach(() => vi.restoreAllMocks());

  it('reads the bank and an entry, proposes, processes, polls, discards, decides and deletes', async () => {
    const client = createHttpClient();
    let fetched = respond(200, FINDER.overview);
    expect(await client.finderOverview()).toEqual({ kind: 'ok', value: FINDER.overview });
    expect(fetched.mock.calls.at(-1)?.[0]).toBe('/api/finder/entries');

    fetched = respond(200, FINDER.proposed);
    expect(await client.finderPropose()).toEqual({ kind: 'ok', value: FINDER.proposed });
    expect(fetched.mock.calls.at(-1)?.[0]).toBe('/api/finder/scenarios');
    expect(JSON.parse(String(fetched.mock.calls.at(-1)?.[1]?.body))).toEqual({ count: 1 });

    const frame = FINDER.progress.confirmed[0]!;
    fetched = respond(202, frame);
    expect(await client.finderProcess(1)).toEqual({ kind: 'ok', value: frame });
    expect(fetched.mock.calls.at(-1)?.[0]).toBe('/api/finder/scenarios/1/process');
    fetched = respond(200, frame);
    expect(await client.finderProgress(9)).toEqual({ kind: 'ok', value: frame });
    expect(fetched.mock.calls.at(-1)?.[0]).toBe('/api/finder/runs/9');

    fetched = respond(200, FINDER.entries.discarded);
    expect(await client.finderDiscard(8)).toEqual({ kind: 'ok', value: FINDER.entries.discarded });
    expect(fetched.mock.calls.at(-1)?.[0]).toBe('/api/finder/scenarios/8/discard');

    fetched = respond(200, FINDER.entries.approved);
    expect(await client.finderDecide(1, 'approved', '  Holds.  ')).toEqual({
      kind: 'ok',
      value: FINDER.entries.approved,
    });
    const [url, init] = fetched.mock.calls.at(-1)!;
    expect(url).toBe('/api/finder/entries/1/decision');
    expect(JSON.parse(String(init?.body))).toEqual({ decision: 'approved', note: 'Holds.' });

    fetched = respond(204, null);
    expect(await client.finderDelete(3)).toEqual({ kind: 'ok', value: null });
    expect(fetched.mock.calls.at(-1)?.[0]).toBe('/api/finder/entries/3');
    expect(fetched.mock.calls.at(-1)?.[1]?.method).toBe('DELETE');

    respond(200, FINDER.entries.rejected);
    expect(await client.finderEntry(5)).toEqual({ kind: 'ok', value: FINDER.entries.rejected });
  });

  it('turns a refusal, a missing run, an unreadable body and an unreachable server into failures', async () => {
    const client = createHttpClient();
    respond(409, API_ERROR_FINDER_BUSY);
    expect(await client.finderPropose()).toEqual({
      kind: 'failure',
      failure: { reason: 'busy', message: API_ERROR_FINDER_BUSY.message },
    });
    respond(404, { code: 'not_found', message: 'This server holds no progress for that run.', fields: [] });
    expect(await client.finderProgress(4)).toMatchObject({ kind: 'failure', failure: { reason: 'missing' } });
    respond(200, { entries: 'no' });
    expect(await client.finderOverview()).toMatchObject({ kind: 'failure', failure: { reason: 'unreadable' } });
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'));
    expect(await client.finderEntry(1)).toMatchObject({ kind: 'failure', failure: { reason: 'network' } });
  });
});

describe('formatClause', () => {
  it('breaks a clause at the ANDs and ORs outside parentheses and quotes, and nowhere else', () => {
    expect(formatClause("a = 1 and b IN (SELECT x FROM t WHERE p = 1 AND q = 2) OR c = ' AND '")).toBe(
      "a = 1\nAND b IN (SELECT x FROM t WHERE p = 1 AND q = 2)\nOR c = ' AND '",
    );
    expect(formatClause('  amount_usd_cents > 100  ')).toBe('amount_usd_cents > 100');
    expect(formatClause('brand = 1')).toBe('brand = 1');
  });
});
