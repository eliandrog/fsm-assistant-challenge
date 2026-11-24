import { API_ERROR_INVALID, HEALTH, TURNS } from '../test/fixtures';
import { isHealth, isTurnResult } from './guards';
import { createHttpClient } from './httpClient';

function respond(status: number, body: unknown, raw = false) {
  const fetchMock = vi.fn<typeof fetch>(
    async () =>
      new Response(raw ? String(body) : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': raw ? 'text/html' : 'application/json' },
      }),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe('the HTTP client', () => {
  it('posts the conversation id and the message, and returns the turn result', async () => {
    const fetchMock = respond(200, TURNS.ruleProposed);
    const reply = await createHttpClient().sendMessage('conversation-1', 'A rule for Rome');

    expect(reply).toEqual({ kind: 'turn', result: TURNS.ruleProposed });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/chat');
    expect(init?.method).toBe('POST');
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init?.body))).toEqual({
      conversation_id: 'conversation-1',
      message: 'A rule for Rome',
    });
  });

  it('returns a turn that failed inside the assistant as a turn, not as a failure', async () => {
    respond(200, TURNS.failedModel);
    const reply = await createHttpClient().sendMessage('c', 'q');
    expect(reply.kind === 'turn' && reply.result.status).toBe('error');
  });

  it.each([
    [422, API_ERROR_INVALID, 'invalid', 'The message is empty.'],
    [409, { code: 'conversation_busy', message: 'Still busy.', fields: [] }, 'busy', 'Still busy.'],
    [500, { code: 'internal', message: 'The server could not handle the request.', fields: [] }, 'server', 'The server could not handle the request.'],
    [500, { unexpected: true }, 'server', 'The server answered with HTTP 500.'],
    [502, 'Bad Gateway', 'network', /Could not reach the assistant’s server/],
    [200, { status: 'ok' }, 'unreadable', 'The server sent a reply this interface could not read.'],
  ] as const)('turns HTTP %i into a failure the UI can show', async (status, body, reason, message) => {
    respond(status, body, typeof body === 'string');
    const reply = await createHttpClient().sendMessage('c', 'q');
    expect(reply.kind).toBe('failure');
    if (reply.kind !== 'failure') return;
    expect(reply.failure.reason).toBe(reason);
    expect(reply.failure.message).toEqual(expect.stringMatching(message));
  });

  it('reports a server that cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const client = createHttpClient();
    const reply = await client.sendMessage('c', 'q');
    expect(reply).toMatchObject({ kind: 'failure', failure: { reason: 'network' } });
    expect(await client.health()).toBeNull();
  });

  it('rejects when the request is aborted', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new DOMException('Aborted', 'AbortError')));
    await expect(createHttpClient().sendMessage('c', 'q')).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('reads the health of the server, or null when it is not one', async () => {
    const fetchMock = respond(200, HEALTH.demo);
    expect(await createHttpClient().health()).toEqual(HEALTH.demo);
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/health');
    respond(200, '<!doctype html>', true);
    expect(await createHttpClient().health()).toBeNull();
    respond(404, { code: 'not_found', message: 'x', fields: [] });
    expect(await createHttpClient().health()).toBeNull();
  });
});

describe('the shape checks', () => {
  it('accept every body the backend returns', () => {
    for (const [name, turn] of Object.entries(TURNS)) expect(isTurnResult(turn), name).toBe(true);
    for (const [name, health] of Object.entries(HEALTH)) expect(isHealth(health), name).toBe(true);
  });

  it('refuse a body of another shape', () => {
    const { meta, ...withoutMeta } = TURNS.answeredTable;
    expect(meta.turn).toBe(1);
    expect(isTurnResult(withoutMeta)).toBe(false);
    expect(isTurnResult({ ...TURNS.answeredTable, table: { columns: 'x' } })).toBe(false);
    expect(isTurnResult({ ...TURNS.answeredTable, suggestions: [1] })).toBe(false);
    expect(isTurnResult(API_ERROR_INVALID)).toBe(false);
    expect(isTurnResult(null)).toBe(false);
    expect(isHealth(TURNS.answeredTable)).toBe(false);
  });
});
