import { useEffect, useState } from 'react';

function prefersReducedMotion(): boolean {
  return (
    typeof window.matchMedia !== 'function' ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}

const TICK_MS = 24;
const TARGET_TICKS = 26; // whole answer in roughly 600ms, whatever its length

/**
 * Reveals text word by word once, on mount (pass animate=false for replies
 * that are not new: restored from storage or revisited). The response arrives whole (the
 * contract is not streaming); this only softens its arrival. Skipped entirely
 * under prefers-reduced-motion.
 */
export function useReveal(text: string, animate = true): { shown: string; done: boolean } {
  const [words] = useState(() => text.split(/(\s+)/));
  const [count, setCount] = useState(() =>
    animate && !prefersReducedMotion() ? 0 : words.length,
  );
  const done = count >= words.length;

  useEffect(() => {
    if (done) return;
    const step = Math.max(2, Math.ceil(words.length / TARGET_TICKS));
    const timer = setInterval(() => setCount((c) => c + step), TICK_MS);
    return () => clearInterval(timer);
  }, [done, words.length]);

  return { shown: done ? text : words.slice(0, count).join(''), done };
}
