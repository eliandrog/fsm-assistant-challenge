import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatClient } from '../api/client';
import type { Health } from '../api/types';

/** `loading` until the first answer; `unreachable` when the server did not give one. */
export type HealthState =
  | { state: 'loading' }
  | { state: 'unreachable' }
  | { state: 'known'; health: Health };

const REFRESH_MS = 30_000;

/**
 * What the server says about itself: asked when the page opens, when the
 * window regains focus, every half minute while it is visible, and whenever
 * `refresh` is called (after a question failed to reach the server).
 */
export function useHealth(client: ChatClient): { health: HealthState; refresh: () => void } {
  const [health, setHealth] = useState<HealthState>({ state: 'loading' });
  const latest = useRef(0);

  const refresh = useCallback(() => {
    const request = ++latest.current;
    void client.health().then((found) => {
      if (request !== latest.current) return;
      setHealth(found ? { state: 'known', health: found } : { state: 'unreachable' });
    });
  }, [client]);

  useEffect(() => {
    refresh();
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh();
    };
    const timer = setInterval(onVisible, REFRESH_MS);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      latest.current += 1;
      clearInterval(timer);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh]);

  return { health, refresh };
}
