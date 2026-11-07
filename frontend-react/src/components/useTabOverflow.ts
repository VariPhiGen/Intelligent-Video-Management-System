/**
 * useTabOverflow — a tab bar that scrolls sideways without showing a scrollbar.
 *
 * Hiding the scrollbar alone hides tabs: nothing says more exist. So this
 * reports which ends have tabs out of view (the bar fades there), turns a
 * vertical mouse wheel into a sideways scroll, and keeps the selected tab in
 * view when it changes — including a deep link straight to the last tab.
 */
import { useEffect, useRef, useState } from 'react';

export function useTabOverflow<T extends HTMLElement>(active: string) {
  const ref = useRef<T>(null);
  const [edges, setEdges] = useState({ start: false, end: false });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const max = el.scrollWidth - el.clientWidth;
      setEdges({ start: el.scrollLeft > 1, end: el.scrollLeft < max - 1 });
    };
    const wheel = (e: WheelEvent) => {
      // Only a mostly-vertical wheel on a bar that can scroll sideways.
      if (el.scrollWidth <= el.clientWidth || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      e.preventDefault();
      el.scrollLeft += e.deltaY;
    };
    measure();
    el.addEventListener('scroll', measure, { passive: true });
    el.addEventListener('wheel', wheel, { passive: false });
    const resize = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null;
    resize?.observe(el);
    return () => {
      el.removeEventListener('scroll', measure);
      el.removeEventListener('wheel', wheel);
      resize?.disconnect();
    };
  }, []);

  useEffect(() => {
    const tab = ref.current?.querySelector<HTMLElement>('.tab.active');
    tab?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [active]);

  const className = `${edges.start ? ' fade-start' : ''}${edges.end ? ' fade-end' : ''}`;
  return { ref, className };
}
