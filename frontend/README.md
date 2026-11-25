# FSM Assistant: web chat

The web interface of the Fraud Success Manager assistant. An analyst asks a question about the
transaction data in plain English and gets back a short answer, the SQL that ran, the result
table and, when asked, a proposed fraud rule with its backtest or a scan of where labelled fraud
is concentrated.

**Status: built against the real API.** The page sends each message to the backend in this
repository (`fsm-assistant serve`) and renders the `TurnResult` it returns. It computes nothing:
every figure on the page was computed by the backend from executed SQL. Its behaviour is covered
by tests that read real response bodies of the API. It has been run over HTTP against the
backend's offline demo, and reviewed there in a real browser, 1400px wide, in the dark theme: the
start screen, the scan answer with its table, notes, caveats and suggestion chips, and the rule
card with the month table and the weakest month highlighted. A width problem in the scan table
was found there and fixed. It has also been run live against `claude-haiku-5-5` through the
browser: six turns in two conversations, each answered with one model request (a query, a
clarification, a declined delete, the scan, and a rule proposed from the scan's first suggestion
chip; listed under [Evidence](../docs/ARCHITECTURE.md#a-live-run-of-the-web-chat)). That is a
handful of questions asked once on one model, at the same width and in the same theme: not an
evaluation. A screenshot of one of those answers is at
[`docs/img/web-chat.png`](../docs/img/web-chat.png).

"Kestrel" is a placeholder product name used in the UI.

The decision behind it is [D-31](../docs/DECISIONS.md#d-31-two-interfaces-over-one-result-the-terminal-and-a-local-web-chat-over-an-http-api);
the API is described under [Interfaces](../docs/ARCHITECTURE.md#interfaces).

**The "Rule finder" tab** is a second view, reached from the top bar, in which nothing of the chat
is shown or reachable; the chat's state is kept, so a draft and a conversation survive the switch.
It works in two steps: "Find a rule" asks the backend for one scenario
(`POST /api/finder/scenarios`), and "Process" on a proposed scenario starts the rule workflow
(`POST /api/finder/scenarios/{id}/process`), whose progress the page polls once a second
(`GET /api/finder/runs/{id}`) and shows step by step. It also discards a scenario, reads the bank
and an entry, records an approval or a rejection, and deletes an entry after a confirmation on the
page. An opened entry is rendered beside the table and reads from the answer down: the backend's
one-sentence verdict, the rule clause by itself (`formatClause` breaks it at its top-level ANDs and
ORs), the scenario, the assumptions and the checks, with the tables and the SQL closed below. Its code is in `src/components/finder/` (`RuleFinder`, `RunProgressPanel`, `FinderEntry`,
`DeleteButton`), `src/hooks/useFinder.ts` and `src/lib/finder.ts`, its styles in
`src/styles/finder.css`, and its tests in `src/components/finder/RuleFinder.test.tsx`, which read
the `finder_*.json` fixtures written by the backend's test (the `finder_progress_*.json` files hold
every progress body of one processing run, in order). What it shows and what its "confirmed" means
are described in the root [README](../README.md#rule-finder). It was driven in headless Chromium in
the offline demo, 1400px wide in the dark theme and 390px wide in the light theme; it has not been
used in a browser with a live model.

## Run

Requires Node 22.12+ (or 24+) and npm. The backend needs [uv](https://docs.astral.sh/uv/); see the
root [README](../README.md#setup).

**Built, one address.** The backend serves the built page and the API together.

```bash
cd frontend && npm ci && npm run build && cd ..
uv run fsm-assistant serve --offline-demo    # free: canned model replies, no key
uv run fsm-assistant serve                   # PAID: each question calls Claude
# open http://127.0.0.1:8000/
```

**Development.** Two terminals. The Vite server reloads the page as it is edited and forwards
`/api` to the backend, so the browser only talks to one origin and the backend needs no CORS.

```bash
uv run fsm-assistant serve --offline-demo    # terminal 1, from the repository root
cd frontend && npm ci && npm run dev         # terminal 2: http://127.0.0.1:5173/
```

`BACKEND_URL` (default `http://127.0.0.1:8000`, in `.env.local`; see `.env.example`) tells the
dev server where the backend listens. It must be a loopback name: the backend refuses requests
addressed to it under any other host.

| Script | What it does |
|---|---|
| `npm run dev` | Vite dev server on `127.0.0.1:5173`, with the `/api` proxy |
| `npm run build` | Type-check, then production build into `dist/`, which `fsm-assistant serve` serves |
| `npm run typecheck` | `tsc -b` (strict) |
| `npm run lint` | ESLint |
| `npm test` | Vitest + Testing Library, run once; needs no server |

The server is for local use only: it has no authentication. So is this page.

## The contract

One request, one response, both defined by the backend's pydantic models.

- `POST /api/chat` with `{ "conversation_id": "...", "message": "..." }` returns a `TurnResult`.
  A turn that failed inside the assistant is still HTTP 200, with `status: "error"`.
- A request the server refuses (422 for an invalid body, 409 for a conversation that stayed
  busy, 5xx) carries an `ApiError`: `{ code, message, fields }`.
- `GET /api/health` returns a `Health`: database, key, model, limits, tracing, mode.

**The types are generated, not written.** `src/api/types.gen.ts` is produced from the JSON schema
of those models by `src/fsm_assistant/typescript.py`. `src/api/types.ts` re-exports them under
the names the components use and adds the two types that exist only in the browser (`Reply`,
`ClientFailure`). Field names are snake_case, as the API sends them.

**The test fixtures are real responses.** `src/test/fixtures/*.json` are bodies returned by the
API in a backend test (`tests/test_web_contract.py`), one per kind of result. That test
regenerates the types and the fixtures and fails when a committed file differs, so the page and
the backend cannot drift unnoticed. After a change to `TurnResult` or another API body:

```bash
FSM_UPDATE_WEB_FIXTURES=1 uv run pytest tests/test_web_contract.py   # from the repository root
cd frontend && npm run typecheck && npm test
```

`tsc` then points at every component that has to learn about the change.

## What the page renders

Everything a `TurnResult` holds, by status.

| Status | Presentation |
|---|---|
| `ok` | The answer line, then the parts below |
| `needs_clarification` | The assistant's question, with its suggestions as buttons |
| `cannot_answer` | A neutral callout with the reason, marked as the model's when it is |
| `rejected` | A "Blocked by the SQL safety guard" callout with the guard's reason; the refusal codes are in the footer, with the number of write attempts the guard stopped when there were any |
| `error` | An error callout titled by the kind of error, SQLite's own words when there are any, and Retry |

The parts, in order, each shown only when the result has it:

- **SQL that ran**, syntax-coloured, with a copy button. A statement longer than twelve lines
  starts collapsed (the scan's profile statement is about seventy).
- **The result table**, with the backend's format hint per column: `usd_cents` as dollars,
  `fraction` as a percentage, `identifier` without digit grouping, `plain` grouped. `NULL` is
  shown as such. The row count is in the header, "First N rows" and a notice when the result was
  capped. 25 rows are rendered until the rest is asked for.
  - **A fraction** is shown as `85.106%`: the percentage the terminal prints in brackets, to the
    same three decimals. The terminal prints the value first (`0.851064 (85.106%)`) so that a
    mis-scaled value cannot pass as a percentage; here the value the query returned is in the
    cell's tooltip ("Value returned: 0.851063829787234") and in the copied table, and a line under
    the table says so. Only the `fraction` hint makes a percentage, and only for a value between
    0 and 1: anything else under that hint is shown as a plain number. A column with no such hint
    (the scan's `lift`) stays a plain number.
  - **Copy** in the table's header copies every row as tab-separated text with the values as
    returned: fractions, cents, no digit grouping.
  - **Width.** The panel is as wide as the message column and grows to the right, up to 56rem or
    the edge of the pane, only when its table needs the room. Before the table scrolls, text
    cells longer than 14 characters wrap (to four lines at most, with the whole text as a tooltip
    beyond 60 characters); numbers never wrap. When it still does not fit, it scrolls sideways
    inside its panel: the first column stays in place, the edge with more columns fades, and a
    line under the table says "Scroll sideways for more columns". The rule card's month table
    uses the same box, and its precision cells carry their value as a tooltip too.
- **The rule card**: the `WHERE` clause; fraud caught, false positives, precision and recall; how
  much was flagged and how much of it has a label; lift over the base fraud rate; amounts; the
  month-by-month table with the weakest month highlighted; the weakest month, the range of
  monthly precision, the earlier and the later period; and the fixed sentence that this is a
  stability check, not a test on unseen data.
- **Notes**: what the fraud scan says besides its table (scope, support floor, segments that are
  the same transactions, what is left without the top segment).
- **Caveats**, each a callout with its code (`label_coverage`, `truncated`, `weak_month`, ...).
- **Model text**: the model's explanation and assumptions, under a label saying they were
  written by the model and not checked against the data.
- **Suggestions** as buttons that send their text as the next message. After a scan the first
  one is "Propose a rule for transactions with ...", which takes a finding to a backtested rule
  in one click. Only the newest answer's buttons are active.
- **A footer**: the tool the model chose, refusal codes if any, the number of write attempts the
  guard stopped when it is above zero (`write_attempts`), the model id, the number of
  model requests, tokens in and out, and the time.

Around a result:

- **Loading**: a "Working on it" row while a turn runs (a rule takes a few seconds, the first
  scan about four), with a Stop button in place of Send.
- **No result**: when the server cannot be reached, refuses the request or sends something
  unreadable, an error callout with the server's own sentence where there is one, and Retry.
- **Server status** in the header, from `/api/health`: the model in use, or "Offline demo", "No
  API key", "Database missing", "Server unreachable"; and "Tracing on" when turns are sent to
  LangSmith. It is asked for when the page opens, when the window regains focus, every half
  minute, and after a request fails.
- **Offline demo**: the empty state lists the questions the demo can answer, and each of its
  answers carries an "Offline demo" badge.
- **Message length**: the composer takes the server's limit from `/api/health`, shows a counter
  near it and does not send past it.

**Text that code did not write is never rendered as HTML.** The model's words and database values
are React text nodes; nothing uses `dangerouslySetInnerHTML`. `lib/text.ts` also writes out what
a text node would hide: control characters (`\x1b`), and zero-width and direction-changing
characters. A test renders a result full of markup and control characters and checks both.

## Conversations and storage

The left sidebar lists conversations, most recent first, titled from their first message. Each
has its own messages and its own `conversation_id`, a UUID made in the browser, which is the key
of the assistant's memory on the server. "New Chat" starts a new id. A new chat joins the list
when its first message is sent. Deleting one offers Undo for a few seconds. There is no rename.

- **Requests are tracked per conversation.** A reply is appended to the conversation that asked,
  whichever one is on screen when it arrives; that conversation shows a dot in the sidebar while it
  waits and when an unseen reply has landed. Stop abandons only the request of the conversation
  on screen. Deleting a conversation abandons its request.
- **Stop does not stop the server.** The turn runs to its end there, is billed, and is remembered.
  A question sent straight after waits for it on the server and is answered next.
- **Saved in `localStorage`** under `kestrel:conversations:v2` as
  `{ conversations: [{ id, title, messages, updatedAt }], activeId }`, where an assistant message
  holds the whole reply, result rows included. The 50 most recent conversations are kept. The
  last active conversation is restored on reload. The suffix is the schema version: change it
  when the shape changes and old data is ignored.
- **This is where results are kept**: in this browser, on this machine. The server writes no
  result anywhere. Deleting a conversation or clearing the site's data removes them.
- **Not saved:** pending requests, unread markers, the Undo offer, unsent drafts. A question whose
  reply was cut off by a reload comes back with a "No reply arrived" row and Retry.
- **Storage is optional.** Every read and write is wrapped; if storage is blocked, full or holds
  unreadable data, the app runs with in-memory conversations. Stored data is validated on load and
  a conversation of another shape is dropped without affecting the others.
- **When the server restarts**, its memory of every conversation is gone; the page still shows
  each transcript. The next question in a conversation is answered as a first turn, without the
  earlier ones as context, and that answer carries a line saying so (the page sees the server
  number the turn 1 again).
- Other keys: `kestrel:sidebar-collapsed:v1` (sidebar collapsed on wide screens) and
  `kestrel-theme`. Tabs do not sync with each other: the last one to write wins.

On screens up to 860px wide the sidebar is a drawer: it starts closed, the page behind is inert
while it is open, Escape or the backdrop closes it, and focus returns to the toggle.

## Structure

```
frontend/
├── index.html               entry; sets the saved theme before first paint
├── vite.config.ts           dev proxy for /api, Vitest config
├── .env.example
└── src/
    ├── main.tsx
    ├── App.tsx              shell: sidebar, header, thread or empty state, composer, live region
    ├── App.test.tsx         behaviour tests through a fixture-backed client
    ├── api/
    │   ├── types.gen.ts     GENERATED from the backend's models; do not edit
    │   ├── types.ts         names for the generated types, plus Reply and ClientFailure
    │   ├── client.ts        ChatClient: sendMessage and health
    │   ├── httpClient.ts    POST /api/chat, GET /api/health; every failure becomes a Reply
    │   ├── httpClient.test.ts
    │   └── guards.ts        shape checks for JSON the page did not write
    ├── hooks/
    │   ├── useConversations.ts  all conversations, per-conversation requests,
    │   │                        send / retry / stop / select / new / delete / undo
    │   ├── useHealth.ts     what /api/health says, kept fresh
    │   ├── useSidebar.ts    collapse (wide) and drawer (narrow) behaviour, focus handling
    │   ├── useMediaQuery.ts
    │   ├── useTheme.ts      light/dark, follows the system until toggled
    │   ├── useReveal.ts     word-by-word reveal of an answer
    │   └── useCopy.ts
    ├── lib/
    │   ├── conversations.ts conversation types, validation, load/save
    │   ├── storage.ts       localStorage wrapper that never throws
    │   ├── format.ts        cents → dollars, fractions → percentages, counts, by the backend's
    │   │                    format hint; the table as tab-separated text
    │   ├── text.ts          makes control and invisible characters visible
    │   ├── evidence.ts      the fixed sentence of the month table; the demo's model id
    │   └── highlightSql.ts  display-only SQL tokenizer
    ├── components/          Header, HealthBadge, Sidebar, EmptyState, Composer, MessageList,
    │                        AssistantMessage (+ test), SqlBlock, SqlCode, ResultsTable,
    │                        TableScroll, RuleCard, CopyButton, Thinking, icons
    ├── styles/              tokens.css, base.css, app.css, sidebar.css
    └── test/
        ├── setup.ts
        ├── fixtures/        response bodies written by the backend's contract test
        ├── fixtures.ts      the fixtures, typed
        └── mockClient.ts    a ChatClient for tests that answers from the fixtures
```

There is no mock mode: the page always talks to the API. The mock client exists for tests only,
and the way to try the page without a key is the backend's `--offline-demo`.

## Design decisions

- **One seam to the backend.** Components only know `ChatClient`. `sendMessage` resolves with a
  `Reply` for every outcome: a turn result, or a failure to get one. The UI has a single rendering
  path, and a test replaces the client, not `fetch`.
- **The page renders; it does not compute or decide.** Figures, format hints, caveats and the
  outcome all come from the backend. The few derived values are ratios of two numbers already on
  the card (legitimate transactions flagged per fraud caught).
- **Model text is kept apart from computed results**, labelled, and placed after them, because
  this tool's value is that its numbers come from executed SQL.
- **Plain CSS with custom-property tokens**, no CSS framework or component library. The surface is
  small, theming needs variables anyway, and it keeps runtime dependencies to React alone. Colours
  are `light-dark()` pairs: the system preference applies by default and the toggle overrides it
  through `color-scheme`. `light-dark()` needs a 2024-or-later browser.
- **No runtime dependencies beyond React.** The SQL highlighter is a ~50-line tokenizer instead of
  a highlighting library; icons are inline SVG; fonts are the system stack. Nothing is fetched from
  a CDN and there is no analytics.
- **SQL is shown next to its result**, so the analyst can check that the query answers the
  question that was asked; only a long statement starts collapsed.
- **The rule card leads with counts, then rates.** With fraud at about 0.18% of labelled
  transactions, precision and recall alone mislead; the card also states how many flagged
  transactions had a label at all and how many legitimate ones are flagged per fraud caught.
- **Long results stay light.** The table renders 25 rows until the analyst asks for the rest, and
  scrolls inside its panel with a sticky header. Expanded SQL scrolls inside its box as well, both
  ways, and is at most 28rem tall.
- **The answer reveal is cosmetic.** The response is not streamed; the text is revealed over about
  0.6 s and the SQL, table and rule follow. Under `prefers-reduced-motion` everything appears at
  once and movement is removed.
- **Accessibility.** Skip link, labelled controls, visible focus, Enter to send and Shift+Enter
  for a new line, keyboard-scrollable code and table regions, a polite live region that announces
  the gist of each reply (not the whole table), status pills that say in words what their colour
  says, 44px touch targets on coarse pointers.
- **Delete uses Undo, not a confirmation dialog.** It keeps the common case to one click and still
  makes the action reversible.
- **The sidebar collapses instantly on wide screens** (a frequent action, and animating its width
  would reflow the thread); only the narrow-screen drawer slides.
- **Only a reply that arrives while its conversation is on screen is revealed progressively.**
  Restored and revisited replies render at once.
- **Left out on purpose:** login, server-side history, renaming or searching conversations,
  markdown rendering of answers, streaming, charts, accept or reject on a rule.

## Known gaps

- Against Claude it has been run on `claude-haiku-5-5` only: six turns, each asked once, by us.
  That says nothing about other questions, and `claude-opus-5-5` has not been run through the
  page.
- Reviewed in a browser at one width and in one theme only (1400px, dark): in the offline demo
  the start screen, the scan answer and the rule card, and in the live run a result table, a
  clarification, a declined request, the scan answer and the rule card. The light theme, narrow
  and mobile widths, the drawer and the other statuses (a rejection, a failure) have been
  checked by tests, not by eye.
- History lives in one browser's `localStorage`: no sync across tabs or devices, and clearing site
  data removes it. It holds result rows.
- A value JSON cannot carry exactly is approximated: an integer beyond 2^53 loses its last digits,
  a blob is shown as base64 text, an infinite float arrives as `null` and is shown as `NULL`.
- A very wide table scrolls sideways inside its panel; a long text cell is cut at 300 characters
  and shown on four lines at most.
- The raw value of a percentage is in a tooltip, which a touch screen does not show; there the
  Copy button is the way to it.
- The table's layout was first worked out by arithmetic on measured text widths (the scan's seven
  columns need about 800px unwrapped and about 690px with its text wrapped). Looking at the scan
  table in a browser at 1400px showed a width problem, which was fixed; other widths have not
  been looked at.
- The drawer and focus behaviour are tested in jsdom, which does not implement `inert`; the tests
  assert the attribute only.
- Tests cover behaviour in jsdom; there are no browser-level or visual regression tests.
