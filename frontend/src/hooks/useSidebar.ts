import { useCallback, useEffect, useRef, useState } from 'react';
import { readJson, writeJson } from '../lib/storage';
import { useMediaQuery } from './useMediaQuery';

const COLLAPSED_KEY = 'kestrel:sidebar-collapsed:v1';
/** Keep in sync with the drawer breakpoint in styles/sidebar.css. */
const DRAWER_QUERY = '(max-width: 860px)';

/**
 * Two modes, one toggle. Wide screens: a column that collapses, remembered
 * across reloads. Narrow screens: an off-canvas drawer that starts closed,
 * makes the rest of the page inert while open, closes on Escape and hands
 * focus back to the toggle.
 */
export function useSidebar() {
  const isDrawer = useMediaQuery(DRAWER_QUERY);
  const [collapsed, setCollapsed] = useState(() => readJson(COLLAPSED_KEY) === true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);

  const visible = isDrawer ? drawerOpen : !collapsed;
  const modal = isDrawer && drawerOpen;

  // Where focus goes when the drawer closes. Applied after the page behind
  // stops being inert; focusing an inert element does nothing.
  const focusAfterClose = useRef<HTMLElement | null>(null);

  /** Closes the drawer. Focus returns to the toggle unless another target is given. */
  const closeDrawer = useCallback((focusTarget?: HTMLElement | null) => {
    focusAfterClose.current = focusTarget ?? toggleRef.current;
    setDrawerOpen(false);
  }, []);

  const toggle = useCallback(() => {
    if (isDrawer) {
      setDrawerOpen((open) => !open);
    } else {
      setCollapsed((value) => {
        writeJson(COLLAPSED_KEY, !value);
        return !value;
      });
    }
  }, [isDrawer]);

  // While the drawer is open: focus moves into it, Escape closes it, the page behind does not scroll.
  useEffect(() => {
    if (!modal) return;
    sidebarRef.current?.querySelector<HTMLElement>('button')?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDrawer();
    };
    document.addEventListener('keydown', onKeyDown);
    const { overflow } = document.body.style;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = overflow;
      focusAfterClose.current?.focus();
      focusAfterClose.current = null;
    };
  }, [modal, closeDrawer]);

  return { isDrawer, visible, modal, toggle, closeDrawer, toggleRef, sidebarRef };
}
