import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import type { ChatClient } from './api/client';
import type { Reply, TurnResult } from './api/types';
import { STORAGE_KEY } from './lib/conversations';
import { HEALTH, TURNS } from './test/fixtures';
import { createMockClient, type MockClient } from './test/mockClient';

const COUNT = 'How many transactions are there?';
const TABLE = 'Fraud rate by card type and merchant';
const SCAN = 'Where is fraud concentrated?';
const RULE = 'Propose a rule for transactions with merchant city and state = Rome, Italy.';

/** Answers from the backend's fixtures, by the exact question. */
function fixtureClient(options: Parameters<typeof createMockClient>[0] = {}): MockClient {
  return createMockClient({
    replies: {
      [COUNT]: TURNS.answeredScalar,
      [TABLE]: TURNS.answeredTable,
      [SCAN]: TURNS.scan,
      [RULE]: TURNS.ruleProposed,
      'Show me the risky ones.': TURNS.clarification,
      'Transactions labelled as fraud': TURNS.answeredTable,
    },
    ...options,
  });
}

function setup(client: ChatClient = fixtureClient()) {
  const user = userEvent.setup();
  render(<App client={client} />);
  const input = screen.getByRole('textbox', { name: 'Message' });
  const ask = async (text: string) => {
    await user.type(input, `${text}{Enter}`);
  };
  return { user, input, ask };
}

/** The sidebar is `inert` (unreachable) when collapsed or closed; jsdom queries ignore that, so check it directly. */
function sidebarEl() {
  return document.getElementById('sidebar') as HTMLElement;
}

/** Queries scoped to the sidebar's conversation list. */
function history() {
  return within(screen.getByRole('navigation', { name: 'Conversations' }));
}

/** Queries scoped to the visible thread (the screen-reader live region repeats the answer). */
async function thread() {
  return within(await screen.findByRole('list', { name: 'Conversation' }));
}

describe('sending a message', () => {
  it('shows example questions and the server status before anything is sent', async () => {
    setup();
    expect(screen.getByRole('list', { name: 'Example questions' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
    const status = within(screen.getByRole('list', { name: 'Server status' }));
    expect(await status.findByText('claude-haiku-5-5')).toBeInTheDocument();
    expect(status.getByText('Tracing on')).toBeInTheDocument();
  });

  it('sends on Enter, clears the composer and renders the answer, SQL, table and footer', async () => {
    const client = fixtureClient();
    const { input, ask } = setup(client);
    await ask(TABLE);

    expect(input).toHaveValue('');
    expect(client.sent).toHaveLength(1);
    expect(client.sent[0]?.text).toBe(TABLE);
    const visible = await thread();
    expect(visible.getByText(TABLE)).toBeInTheDocument();
    expect(await visible.findByText('The query returned 5 rows.')).toBeInTheDocument();
    // The live region announces the gist to screen readers.
    expect(screen.getByText(/A table with 5 rows follows\./)).toHaveAttribute('aria-live', 'polite');

    const sql = screen.getByRole('region', { name: 'SQL that ran' });
    expect(sql).toHaveTextContent('LEFT JOIN fraud_labels AS f ON f.transaction_id = CAST(t.id AS TEXT)');
    expect(within(sql).getByText('SELECT')).toHaveClass('tok-keyword');

    const table = screen.getByRole('table');
    expect(within(table).getByRole('columnheader', { name: 'fraud_rate' })).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: 'Credit' })).toBeInTheDocument();
    expect(visible.getByText('5 rows')).toBeInTheDocument();
    expect(visible.getByLabelText('How this answer was produced')).toHaveTextContent(
      'tool: run_sqlscripted1 model request1,000 in, 100 out tokens1.2 s',
    );
  });

  it('keeps Shift+Enter as a new line', async () => {
    const { user, input } = setup();
    await user.type(input, 'line one{Shift>}{Enter}{/Shift}line two');
    expect(input).toHaveValue('line one\nline two');
    expect(screen.queryByRole('list', { name: 'Conversation' })).not.toBeInTheDocument();
  });

  it('sends an example question when it is clicked', async () => {
    const client = fixtureClient();
    const { user } = setup(client);
    await user.click(screen.getByRole('button', { name: /Where is fraud concentrated\?/ }));
    expect(await screen.findByRole('cell', { name: 'Rome, Italy' })).toBeInTheDocument();
    expect(client.sent[0]?.text).toBe(SCAN);
  });

  it('expands and collapses the SQL block', async () => {
    const { user, ask } = setup();
    await ask(COUNT);
    const toggle = await screen.findByRole('button', { name: /^SQL that ran/, expanded: true });
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'SQL that ran' })).not.toBeInTheDocument();
  });

  it('refuses a message longer than the server allows, and says so', async () => {
    const client = fixtureClient({
      health: { ...HEALTH.live, limits: { ...HEALTH.live.limits, max_message_characters: 20 } },
    });
    const { user, input } = setup(client);
    await screen.findByText('claude-haiku-5-5');
    await user.type(input, 'x'.repeat(21));
    expect(screen.getByText('21 / 20')).toBeInTheDocument();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
    await user.type(input, '{Enter}');
    expect(client.sent).toHaveLength(0);
    await user.type(input, '{Backspace}{Enter}');
    expect(client.sent).toHaveLength(1);
  });
});

describe('from a finding to a rule', () => {
  it('sends a suggestion of the scan as the next message, in the same conversation', async () => {
    const client = fixtureClient();
    const { user, ask } = setup(client);
    await ask(SCAN);

    const suggestions = await screen.findByRole('list', { name: 'Suggested questions' });
    await user.click(within(suggestions).getByRole('button', { name: RULE }));

    expect(await screen.findByRole('region', { name: 'Proposed rule' })).toBeInTheDocument();
    expect(client.sent.map((sent) => sent.text)).toEqual([SCAN, RULE]);
    expect(client.sent[0]?.conversationId).toBe(client.sent[1]?.conversationId);
    // The chips of the earlier answer can no longer be sent.
    for (const chip of within(suggestions).getAllByRole('button')) expect(chip).toBeDisabled();
  });

  it('asks for clarification and sends the chosen suggestion', async () => {
    const { user, ask } = setup();
    await ask('Show me the risky ones.');

    expect(await (await thread()).findByText('What should count as risky?')).toBeInTheDocument();
    const suggestions = screen.getByRole('list', { name: 'Suggested questions' });
    await user.click(within(suggestions).getByRole('button', { name: 'Transactions labelled as fraud' }));
    expect(await screen.findByRole('table')).toBeInTheDocument();
  });
});

describe('errors and recovery', () => {
  it('shows a failed turn with Retry, and replaces it with the answer when retry succeeds', async () => {
    let calls = 0;
    const client = fixtureClient({
      fallback: () => (++calls === 1 ? TURNS.failedModel : TURNS.answeredTable),
    });
    const { user, ask } = setup(client);
    await ask('anything');

    expect(await screen.findByText('The model didn’t respond')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(screen.queryByText('The model didn’t respond')).not.toBeInTheDocument();
    // The question is not duplicated by the retry.
    expect((await thread()).getAllByText('anything')).toHaveLength(1);
    expect(client.sent.map((sent) => sent.text)).toEqual(['anything', 'anything']);
  });

  it('shows a server that cannot be reached, and asks for its status again', async () => {
    const unreachable: Reply = {
      kind: 'failure',
      failure: { reason: 'network', message: 'Could not reach the assistant’s server.' },
    };
    const client = fixtureClient({ fallback: unreachable });
    const health = vi.spyOn(client, 'health');
    const { user, ask } = setup(client);
    await screen.findByText('claude-haiku-5-5');
    health.mockResolvedValue(null);
    await ask('anything');

    expect(await screen.findByText('Can’t reach the server')).toBeInTheDocument();
    expect(screen.getByText('Could not reach the assistant’s server.')).toBeInTheDocument();
    expect(await screen.findByText('Server unreachable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(client.sent).toHaveLength(2);
  });

  it('turns an unexpected client failure into a retryable error', async () => {
    const sendMessage = vi
      .fn<ChatClient['sendMessage']>()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({ kind: 'turn', result: TURNS.answeredScalar });
    const { user, ask } = setup({ ...createMockClient(), sendMessage, health: async () => HEALTH.live });
    await ask('anything');

    expect(await screen.findByText('The reply could not be read')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await (await thread()).findByText('transactions: 6')).toBeInTheDocument();
    expect(sendMessage).toHaveBeenCalledTimes(2);
    expect(sendMessage.mock.calls[1]?.[1]).toBe('anything');
  });

  it('shows a loading state, can stop a pending request, then start a new chat', async () => {
    const { user, ask } = setup(fixtureClient({ latencyMs: 60_000 }));
    await ask(COUNT);

    expect(await screen.findByText('Working on it')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'Stop waiting for the reply' }));
    expect(await screen.findByText('No reply arrived for this question.')).toBeInTheDocument();
    expect(screen.queryByText('Working on it')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'New Chat' }));
    expect(screen.getByRole('list', { name: 'Example questions' })).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Conversation' })).not.toBeInTheDocument();
    // The conversation is kept in the sidebar.
    expect(history().getByRole('button', { name: COUNT })).toBeInTheDocument();
  });
});

describe('server status', () => {
  it.each([
    ['the offline demo', HEALTH.demo, 'Offline demo'],
    ['a missing key', HEALTH.noKey, 'No API key'],
    ['a missing database', HEALTH.noDatabase, 'Database missing'],
    ['a server that does not answer', null, 'Server unreachable'],
  ])('shows %s', async (_name, health, label) => {
    setup(fixtureClient({ health }));
    const status = within(screen.getByRole('list', { name: 'Server status' }));
    expect(await status.findByText(label)).toBeInTheDocument();
    expect(status.queryByText('Tracing on')).not.toBeInTheDocument();
  });

  it('offers the questions the offline demo can answer, and says they are canned', async () => {
    const client = fixtureClient({ health: HEALTH.demo });
    const { user } = setup(client);
    const examples = within(await screen.findByRole('list', { name: 'Example questions' }));
    expect(await examples.findByRole('button', { name: 'Delete the fraud labels.' })).toBeInTheDocument();
    expect(examples.getAllByRole('button')).toHaveLength(HEALTH.demo.demo_questions.length);
    expect(screen.getByText(/Offline demo: no model is connected/)).toBeInTheDocument();
    expect(screen.getByText(/Offline demo: canned model replies/)).toBeInTheDocument();

    await user.click(examples.getByRole('button', { name: COUNT }));
    expect(client.sent[0]?.text).toBe(COUNT);
  });
});

/** A client whose replies are released by the test, to control ordering. */
function manualClient() {
  const calls: { conversationId: string; text: string; reply: (answer: string, turn?: number) => void }[] = [];
  const client: ChatClient = {
    ...createMockClient(),
    health: async () => HEALTH.live,
    sendMessage: (conversationId, text) =>
      new Promise<Reply>((resolve) => {
        calls.push({
          conversationId,
          text,
          reply: (answer, turn = 1) => {
            const result: TurnResult = {
              ...TURNS.cannotAnswer,
              outcome: 'answered',
              status: 'ok',
              answer,
              meta: { ...TURNS.cannotAnswer.meta, conversation_id: conversationId, turn },
            };
            resolve({ kind: 'turn', result });
          },
        });
      }),
  };
  return { client, calls };
}

describe('conversations', () => {
  it('starts with an empty history and lists a conversation once its first message is sent', async () => {
    const { ask } = setup();
    expect(history().getByText(/No conversations yet/)).toBeInTheDocument();

    await ask(COUNT);

    const item = history().getByRole('button', { name: COUNT });
    expect(item).toHaveAttribute('aria-current', 'page');
  });

  it('gives each conversation its own id, and switches between them', async () => {
    const client = fixtureClient();
    const { user, ask } = setup(client);
    await ask(COUNT);
    await (await thread()).findByText('transactions: 6');

    await user.click(screen.getByRole('button', { name: 'New Chat' }));
    expect(screen.getByRole('list', { name: 'Example questions' })).toBeInTheDocument();
    await ask(SCAN);
    await screen.findByRole('cell', { name: 'Rome, Italy' });
    expect(client.sent[0]?.conversationId).not.toBe(client.sent[1]?.conversationId);
    expect(client.sent[1]?.conversationId).toMatch(/^[A-Za-z0-9_-]{1,64}$/);

    // Most recent first.
    const titles = history()
      .getAllByRole('button')
      .filter((b) => !b.getAttribute('aria-label'))
      .map((b) => b.textContent);
    expect(titles).toEqual([SCAN, COUNT]);

    await user.click(history().getByRole('button', { name: COUNT }));

    const visible = await thread();
    expect(visible.getByText('transactions: 6')).toBeInTheDocument();
    expect(visible.queryByText(SCAN)).not.toBeInTheDocument();
    expect(history().getByRole('button', { name: COUNT })).toHaveAttribute('aria-current', 'page');
    expect(history().getByRole('button', { name: SCAN })).not.toHaveAttribute('aria-current');
  });

  it('routes a late reply to the conversation that asked, not the one on screen', async () => {
    const { client, calls } = manualClient();
    const { user, ask } = setup(client);

    await ask('first question');
    expect(screen.getByRole('button', { name: 'Stop waiting for the reply' })).toBeInTheDocument();

    // Leave while the first request is still pending. Pending state does not follow.
    await user.click(screen.getByRole('button', { name: 'New Chat' }));
    expect(screen.getByRole('button', { name: 'Send message' })).toBeInTheDocument();
    expect(history().getByRole('img', { name: 'Waiting for a reply' })).toBeInTheDocument();

    await ask('second question');
    expect(calls).toHaveLength(2);
    expect(calls[0]?.conversationId).not.toBe(calls[1]?.conversationId);

    // The first reply lands while the second conversation is on screen.
    await act(async () => calls[0]?.reply('Answer to the first.'));
    const second = await thread();
    expect(second.getByText('second question')).toBeInTheDocument();
    expect(second.queryByText('Answer to the first.')).not.toBeInTheDocument();
    expect(history().getByRole('img', { name: 'New reply' })).toBeInTheDocument();
    // The second conversation is still waiting for its own reply.
    expect(screen.getByRole('button', { name: 'Stop waiting for the reply' })).toBeInTheDocument();

    await user.click(history().getByRole('button', { name: /^first question/ }));
    const first = await thread();
    expect(first.getByText('Answer to the first.')).toBeInTheDocument();
    expect(first.queryByText('second question')).not.toBeInTheDocument();
    expect(history().queryByRole('img', { name: 'New reply' })).not.toBeInTheDocument();

    await act(async () => calls[1]?.reply('Answer to the second.'));
    expect(first.queryByText('Answer to the second.')).not.toBeInTheDocument();
    await user.click(history().getByRole('button', { name: /^second question/ }));
    expect((await thread()).getByText('Answer to the second.')).toBeInTheDocument();
  });

  it('deletes a conversation and restores it with Undo', async () => {
    const { user, ask } = setup();
    await ask(COUNT);
    await screen.findByRole('table');

    await user.click(screen.getByRole('button', { name: `Delete conversation: ${COUNT}` }));

    expect(history().getByText(/No conversations yet/)).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Example questions' })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}').conversations).toEqual([]);

    await user.click(screen.getByRole('button', { name: 'Undo' }));

    expect(history().getByRole('button', { name: COUNT })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByRole('table')).toBeInTheDocument();
  });
});

describe('persistence', () => {
  it('restores conversations, their results and the active one after a reload', async () => {
    const first = setup();
    await first.ask(TABLE);
    await screen.findByRole('table');
    await first.user.click(screen.getByRole('button', { name: 'New Chat' }));
    await first.ask(RULE);
    await screen.findByRole('region', { name: 'Proposed rule' });
    await first.user.click(history().getByRole('button', { name: TABLE }));
    cleanup();

    // "Reload" with a server that answers nothing: the transcript needs none.
    const { client, calls } = manualClient();
    setup(client);

    expect(history().getAllByRole('listitem')).toHaveLength(2);
    expect(history().getByRole('button', { name: TABLE })).toHaveAttribute('aria-current', 'page');
    const restored = await thread();
    expect(restored.getByText('The query returned 5 rows.')).toBeInTheDocument();
    expect(restored.getByRole('table')).toBeInTheDocument();
    expect(restored.getByText('label_coverage')).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it('keeps the transcript when the server was restarted, and says the next answer stands alone', async () => {
    const { client, calls } = manualClient();
    const { ask } = setup(client);
    await ask('first question');
    await act(async () => calls[0]?.reply('First answer.', 1));
    await ask('a follow-up');
    await act(async () => calls[1]?.reply('Second answer.', 2));
    expect(screen.queryByText(/The server was restarted/)).not.toBeInTheDocument();

    // The server lost its memory: it numbers the next turn of this conversation 1 again.
    await ask('another follow-up');
    await act(async () => calls[2]?.reply('Third answer.', 1));

    const visible = await thread();
    expect(visible.getByText('First answer.')).toBeInTheDocument();
    expect(visible.getByText('Second answer.')).toBeInTheDocument();
    expect(visible.getByText('Third answer.')).toBeInTheDocument();
    expect(visible.getAllByText(/The server was restarted since the earlier questions/)).toHaveLength(1);
    expect(new Set(calls.map((call) => call.conversationId)).size).toBe(1);
  });

  it('does not store pending state: a question interrupted by a reload offers Retry', async () => {
    const { client } = manualClient();
    const { ask } = setup(client);
    await ask('interrupted question');
    cleanup();

    const next = manualClient();
    const { user } = setup(next.client);

    expect(screen.getByRole('button', { name: 'Send message' })).toBeInTheDocument();
    expect(await screen.findByText('No reply arrived for this question.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(next.calls[0]?.text).toBe('interrupted question');
  });

  it.each([
    ['unparseable JSON', '{not json'],
    ['the wrong shape', JSON.stringify({ conversations: 'nope', activeId: 3 })],
    [
      'a malformed conversation',
      JSON.stringify({ conversations: [{ id: 'x', title: 'Broken', updatedAt: 1, messages: [{ role: 'user' }] }] }),
    ],
    [
      'a reply of another shape',
      JSON.stringify({
        conversations: [
          {
            id: 'x',
            title: 'Old',
            updatedAt: 1,
            messages: [
              { id: 'm1', role: 'user', text: 'Old' },
              { id: 'm2', role: 'assistant', question: 'Old', reply: { kind: 'turn', result: { status: 'ok', answer: 'x' } } },
            ],
          },
        ],
      }),
    ],
  ])('starts clean when storage holds %s', (_name, raw) => {
    localStorage.setItem(STORAGE_KEY, raw);
    setup();
    expect(history().getByText(/No conversations yet/)).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Example questions' })).toBeInTheDocument();
  });

  it('works when storage is unavailable', async () => {
    const blocked = () => {
      throw new DOMException('blocked', 'SecurityError');
    };
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(blocked);
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(blocked);
    try {
      const { ask } = setup();
      await ask(COUNT);
      expect(await screen.findByRole('table')).toBeInTheDocument();
      expect(history().getByRole('button', { name: COUNT })).toBeInTheDocument();
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });
});

describe('sidebar', () => {
  it('collapses on wide screens, remembers it, and moves New Chat to the header', async () => {
    const { user } = setup();
    const toggle = screen.getByRole('button', { name: 'Hide conversations' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await user.click(toggle);

    expect(screen.getByRole('button', { name: 'Show conversations' })).toHaveAttribute('aria-expanded', 'false');
    expect(sidebarEl()).toHaveAttribute('inert');
    expect(within(screen.getByRole('banner')).getByRole('button', { name: 'New Chat' })).toBeInTheDocument();

    cleanup();
    setup();
    expect(screen.getByRole('button', { name: 'Show conversations' })).toBeInTheDocument();
  });

  it('is a modal drawer on narrow screens: closed at first, Escape closes it and returns focus', async () => {
    const original = window.matchMedia;
    window.matchMedia = (query: string) =>
      ({ ...original(query), matches: query.includes('max-width') || query.includes('reduced-motion') }) as MediaQueryList;
    try {
      const { user, ask } = setup();
      await ask(COUNT);
      await screen.findByRole('table');

      const toggle = screen.getByRole('button', { name: 'Show conversations' });
      expect(sidebarEl()).toHaveAttribute('inert');

      await user.click(toggle);
      expect(sidebarEl()).not.toHaveAttribute('inert');
      expect(screen.getByRole('button', { name: 'Close conversations' })).toHaveFocus();
      // The page behind is inert, so focus cannot leave the drawer.
      expect(screen.getByRole('main', { hidden: true }).closest('[inert]')).not.toBeNull();

      await user.keyboard('{Escape}');
      expect(sidebarEl()).toHaveAttribute('inert');
      expect(toggle).toHaveFocus();

      // Choosing a conversation closes the drawer; a new chat also readies the composer.
      await user.click(toggle);
      await user.click(history().getByRole('button', { name: COUNT }));
      expect(sidebarEl()).toHaveAttribute('inert');
      expect(toggle).toHaveFocus();

      await user.click(toggle);
      await user.click(within(sidebarEl()).getByRole('button', { name: 'New Chat' }));
      expect(sidebarEl()).toHaveAttribute('inert');
      expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus();
    } finally {
      window.matchMedia = original;
    }
  });
});
