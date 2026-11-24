import { useCallback, useEffect, useRef, useState } from 'react';

/** Copy text to the clipboard and report success for a short moment. */
export function useCopy(resetMs = 1600) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = useCallback(
    async (text: string) => {
      let ok = true;
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        ok = false;
      }
      setState(ok ? 'copied' : 'failed');
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setState('idle'), resetMs);
    },
    [resetMs],
  );

  return { state, copy };
}
