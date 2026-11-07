/** Finite, responsive placeholders. Only the real loading label is announced;
 * decorative shapes never masquerade as operational measurements. */
export function LoadingSkeleton({ label = 'Loading…', variant = 'rows', count = 4 }: {
  label?: string; variant?: 'rows' | 'cards' | 'preview'; count?: number;
}) {
  return (
    <div className={`loading-skeleton loading-skeleton-${variant}`} role="status" aria-label={label}>
      <span className="sr-only">{label}</span>
      <div className="loading-skeleton-shapes" aria-hidden="true">
        {Array.from({ length: variant === 'preview' ? 1 : count }, (_, i) => (
          <div className="loading-skeleton-item" key={i}>
            {variant !== 'rows' && <div className="skel loading-skeleton-image" />}
            <div className="skel-stack">
              <span className="skel" style={{ width: '58%', height: 14 }} />
              <span className="skel" style={{ width: '82%', height: 10 }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
