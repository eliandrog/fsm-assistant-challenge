import { useCallback, useRef, useState } from 'react';
import { createClient, type ChatClient } from './api/client';
import type { Reply } from './api/types';
import { Composer } from './components/Composer';
import { EmptyState } from './components/EmptyState';
import { Header } from './components/Header';
import { MessageList } from './components/MessageList';
import { Sidebar } from './components/Sidebar';
import { useConversations } from './hooks/useConversations';
import { useHealth } from './hooks/useHealth';
import { useSidebar } from './hooks/useSidebar';
import { useTheme } from './hooks/useTheme';
import type { ChatMessage } from './lib/conversations';
import { safeText } from './lib/text';

/** What a screen reader hears when a reply lands: the gist, not the whole table. */
function describe(reply: Reply | null): string {
  if (!reply) return 'No reply arrived.';
  if (reply.kind === 'failure') return `Error. ${reply.failure.message} Retry is available.`;
  const { result } = reply;
  if (result.status === 'error') return `Error. ${result.error?.message ?? result.answer}`;
  const parts = [safeText(result.answer, 400)];
  if (result.status === 'rejected') parts.unshift('Blocked by the SQL safety guard.');
  if (result.table) parts.push(`A table with ${result.table.row_count} rows follows.`);
  if (result.rule) parts.push('A proposed rule with its backtest follows.');
  if (result.caveats.length > 0) parts.push(`${result.caveats.length} caveats.`);
  return parts.join(' ');
}

function announcement(messages: ChatMessage[], pending: boolean): string {
  if (pending) return 'Working on it…';
  const last = messages[messages.length - 1];
  return last?.role === 'assistant' ? describe(last.reply) : '';
}

export default function App({ client: injected }: { client?: ChatClient }) {
  const [client] = useState(() => injected ?? createClient());
  const { health, refresh: refreshHealth } = useHealth(client);
  const chat = useConversations(client, refreshHealth);
  const sidebar = useSidebar();
  const { theme, toggle: toggleTheme } = useTheme();
  // One unsent draft per conversation, so switching does not carry text across.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const { activeId, messages, pending, send } = chat;
  const draft = drafts[activeId] ?? '';
  const setDraft = useCallback(
    (text: string) => setDrafts((prev) => ({ ...prev, [activeId]: text })),
    [activeId],
  );

  const submit = useCallback(() => {
    send(draft);
    setDraft('');
  }, [draft, send, setDraft]);

  const sendPreset = useCallback(
    (text: string) => {
      send(text);
      inputRef.current?.focus();
    },
    [send],
  );

  const { closeDrawer, modal: drawerOpen } = sidebar;
  const { newChat, select } = chat;

  /** A new chat is for typing: focus goes to the composer, closing the drawer if it is open. */
  const startNewChat = useCallback(() => {
    newChat();
    if (drawerOpen) closeDrawer(inputRef.current);
    else inputRef.current?.focus();
  }, [newChat, drawerOpen, closeDrawer]);

  /**
   * Opening a conversation is for reading. From the drawer, focus returns to
   * the toggle (focusing the composer would raise the on-screen keyboard over
   * the thread); with the sidebar docked, the composer is ready for a follow-up.
   */
  const openConversation = useCallback(
    (id: string) => {
      select(id);
      if (drawerOpen) closeDrawer();
      else inputRef.current?.focus();
    },
    [select, drawerOpen, closeDrawer],
  );

  const isEmpty = messages.length === 0;
  const known = health.state === 'known' ? health.health : null;
  const isDemo = known?.mode === 'offline_demo';

  return (
    <div className="app">
      <a className="skip-link" href="#composer-input">
        Skip to message box
      </a>

      <Sidebar
        conversations={chat.conversations}
        activeId={activeId}
        pendingIds={chat.pendingIds}
        unreadIds={chat.unreadIds}
        removed={chat.removed}
        visible={sidebar.visible}
        isDrawer={sidebar.isDrawer}
        sidebarRef={sidebar.sidebarRef}
        onSelect={openConversation}
        onNewChat={startNewChat}
        onDelete={chat.remove}
        onUndoDelete={chat.undoRemove}
        onClose={() => closeDrawer()}
      />

      {/* Inert while the drawer is open: focus and clicks stay in the drawer. */}
      <div className="pane" inert={sidebar.modal}>
        <Header
          health={health}
          theme={theme}
          onToggleTheme={toggleTheme}
          sidebarVisible={sidebar.visible}
          onToggleSidebar={sidebar.toggle}
          sidebarToggleRef={sidebar.toggleRef}
          onNewChat={startNewChat}
          canReset={!isEmpty}
        />

        <main className="main" data-empty={isEmpty || undefined}>
          <div className="column">
            {isEmpty ? (
              <EmptyState onPick={sendPreset} demoQuestions={known?.demo_questions ?? []} />
            ) : (
              <MessageList
                key={activeId}
                messages={messages}
                pending={pending}
                freshId={chat.freshId}
                onRetry={chat.retry}
                onSuggestion={sendPreset}
              />
            )}
          </div>
        </main>

        <div className="dock">
          <div className="column">
            <Composer
              value={draft}
              onChange={setDraft}
              onSubmit={submit}
              onStop={chat.stop}
              pending={pending}
              inputRef={inputRef}
              maxLength={known?.limits.max_message_characters}
            />
            <p id="composer-hint" className="dock-hint">
              <span className="kbd-hint">
                <kbd>Enter</kbd> to send, <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line.{' '}
              </span>
              {isDemo
                ? 'Offline demo: canned model replies, run through the real SQL guard and database.'
                : 'Check the SQL before relying on a result.'}
            </p>
          </div>
        </div>
      </div>

      <div className="sr-only" role="status" aria-live="polite">
        {announcement(messages, pending)}
      </div>
    </div>
  );
}
