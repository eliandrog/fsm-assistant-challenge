import type { RefObject } from 'react';
import type { Conversation } from '../lib/conversations';
import { CloseIcon, PlusIcon, TrashIcon } from './icons';

interface Props {
  conversations: Conversation[];
  activeId: string;
  pendingIds: ReadonlySet<string>;
  unreadIds: ReadonlySet<string>;
  /** The conversation just deleted, while Undo is on offer. */
  removed: Conversation | null;
  /** False when collapsed (wide screens) or closed (drawer). */
  visible: boolean;
  isDrawer: boolean;
  sidebarRef: RefObject<HTMLElement | null>;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (id: string) => void;
  onUndoDelete: () => void;
  onClose: () => void;
}

export function Sidebar({
  conversations,
  activeId,
  pendingIds,
  unreadIds,
  removed,
  visible,
  isDrawer,
  sidebarRef,
  onSelect,
  onNewChat,
  onDelete,
  onUndoDelete,
  onClose,
}: Props) {
  return (
    <>
      {isDrawer && <div className="backdrop" data-open={visible || undefined} onClick={onClose} />}
      <aside
        id="sidebar"
        ref={sidebarRef}
        className="sidebar"
        data-visible={visible || undefined}
        // Hidden means unreachable: out of the tab order and the accessibility tree.
        inert={!visible}
        aria-label="Conversation history"
      >
        <div className="sidebar-head">
          <h2 className="sidebar-title">Conversations</h2>
          {isDrawer && (
            <button
              type="button"
              className="btn btn-ghost btn-icon"
              onClick={onClose}
              aria-label="Close conversations"
            >
              <CloseIcon />
            </button>
          )}
        </div>

        <button type="button" className="btn btn-outline sidebar-new" onClick={onNewChat}>
          <PlusIcon /> New Chat
        </button>

        <nav className="sidebar-nav" aria-label="Conversations">
          {conversations.length === 0 ? (
            <p className="sidebar-empty">
              No conversations yet. Questions you ask are kept here, in this browser only.
            </p>
          ) : (
            <ul>
              {conversations.map((conversation) => {
                const active = conversation.id === activeId;
                const isPending = pendingIds.has(conversation.id);
                const unread = unreadIds.has(conversation.id);
                return (
                  <li key={conversation.id} className="conv" data-active={active || undefined}>
                    <button
                      type="button"
                      className="conv-open"
                      aria-current={active ? 'page' : undefined}
                      onClick={() => onSelect(conversation.id)}
                    >
                      <span className="conv-title">{conversation.title}</span>
                      {isPending ? (
                        <span className="conv-dot conv-dot-pending" role="img" aria-label="Waiting for a reply" />
                      ) : (
                        unread && <span className="conv-dot" role="img" aria-label="New reply" />
                      )}
                    </button>
                    <button
                      type="button"
                      className="conv-delete"
                      onClick={() => onDelete(conversation.id)}
                      aria-label={`Delete conversation: ${conversation.title}`}
                    >
                      <TrashIcon />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </nav>

        <div className="sidebar-foot" role="status">
          {removed && (
            <div className="undo">
              <span>Conversation deleted</span>
              <button type="button" className="btn btn-ghost btn-small" onClick={onUndoDelete}>
                Undo
              </button>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
