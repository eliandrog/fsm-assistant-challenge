import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

interface Props {
  /** Accessible name of the scrollable region. */
  label: string;
  className?: string;
  children: ReactNode;
}

/**
 * The box a table scrolls in. When the table is wider than the box it says so:
 * the edge with more columns behind it fades, the first column stays in place
 * and a line under the table names the scroll.
 */
export function TableScroll({ label, className, children }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [more, setMore] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const box = ref.current;
    if (!box) return;
    const left = box.scrollLeft > 1;
    const right = box.scrollLeft + box.clientWidth < box.scrollWidth - 1;
    setMore((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const box = ref.current;
    if (!box || typeof ResizeObserver === 'undefined') return;
    // Reports once on observing, then whenever the box or the table changes size.
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    if (box.firstElementChild) observer.observe(box.firstElementChild);
    return () => observer.disconnect();
  }, [measure]);

  return (
    <>
      <div
        className={['table-wrap', className].filter(Boolean).join(' ')}
        data-more-left={more.left || undefined}
        data-more-right={more.right || undefined}
      >
        <div ref={ref} className="table-scroll" tabIndex={0} role="region" aria-label={label} onScroll={measure}>
          {children}
        </div>
      </div>
      {(more.left || more.right) && (
        <p className="scroll-hint">
          <span aria-hidden="true">↔</span> Scroll sideways for more columns
        </p>
      )}
    </>
  );
}
