import type { RefObject } from 'react';
import type { HealthState } from '../hooks/useHealth';
import type { Theme } from '../hooks/useTheme';
import { HealthBadge } from './HealthBadge';
import { BrandMark, MoonIcon, PlusIcon, SidebarIcon, SunIcon } from './icons';

interface Props {
  health: HealthState;
  theme: Theme;
  onToggleTheme: () => void;
  sidebarVisible: boolean;
  onToggleSidebar: () => void;
  sidebarToggleRef: RefObject<HTMLButtonElement | null>;
  /** Shown here only while the sidebar, which has its own, is out of view. */
  onNewChat: () => void;
  canReset: boolean;
}

export function Header({
  health,
  theme,
  onToggleTheme,
  sidebarVisible,
  onToggleSidebar,
  sidebarToggleRef,
  onNewChat,
  canReset,
}: Props) {
  return (
    <header className="topbar">
      <div className="topbar-inner">
        <button
          ref={sidebarToggleRef}
          type="button"
          className="btn btn-ghost btn-icon"
          onClick={onToggleSidebar}
          aria-label={sidebarVisible ? 'Hide conversations' : 'Show conversations'}
          aria-expanded={sidebarVisible}
          aria-controls="sidebar"
        >
          <SidebarIcon />
        </button>
        <h1 className="brand">
          <BrandMark size={24} />
          <span translate="no">Kestrel</span>
          <span className="sr-only"> fraud analysis assistant</span>
        </h1>
        <HealthBadge health={health} />
        <div className="topbar-actions">
          {!sidebarVisible && (
            <button type="button" className="btn btn-ghost" onClick={onNewChat} disabled={!canReset}>
              <PlusIcon /> <span className="topbar-label">New Chat</span>
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onToggleTheme}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          >
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </button>
        </div>
      </div>
    </header>
  );
}
