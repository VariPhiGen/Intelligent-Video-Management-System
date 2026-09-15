/** Modal.tsx — overlay + modal card. Header/footer pinned, body scrolls, card
 *  capped to the viewport so tall content never overflows the screen. */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

export function Modal({ open, title, onClose, width = 520, children, style, footer }: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  width?: number | string;
  children: ReactNode;
  style?: CSSProperties;
  /** Pinned below the scroll area (e.g. a save bar). Optional. */
  footer?: ReactNode;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const [scrolled, setScrolled] = useState(false);

  // Show the header divider only once the body is actually scrolled.
  useEffect(() => {
    const el = bodyRef.current;
    if (!open || !el) return;
    const onScroll = () => setScrolled(el.scrollTop > 2);
    onScroll();
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [open]);

  // Close on Escape — expected of any modal, and the only way out once the
  // close button could scroll away (it no longer can, but Esc is still right).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="overlay open" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      {/* Column layout: header and footer are fixed, only .modal-body scrolls,
          and the whole card is capped to the viewport — so tall content (e.g.
          several encoder profiles) never pushes the header off-screen. */}
      <div className="modal" style={{ width, ...style }} data-scrolled={scrolled}>
        <div className="modal-header">
          <span className="modal-title">{title}</span>
          <button className="close-btn" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="modal-body" ref={bodyRef}>{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
