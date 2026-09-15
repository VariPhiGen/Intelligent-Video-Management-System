/** ComingSoon.tsx — roadmap placeholder pages (prototype module map). */
export function ComingSoon({ glyph, title, subtitle, chips, note }: {
  glyph: string;
  title: string;
  subtitle: string;
  chips: string[];
  note?: string;
}) {
  return (
    <div className="coming-soon fade">
      <div className="cs-glyph">{glyph}</div>
      <span className="badge badge-blue" style={{ marginBottom: 'var(--s4)' }}>
        <span className="badge-dot" />On the roadmap
      </span>
      <h1>{title}</h1>
      <p className="subtitle">{subtitle}</p>
      {/* The chips are the promise: what this surface will actually do. */}
      <div className="cs-chips">
        {chips.map(c => <span key={c} className="chip">{c}</span>)}
      </div>
      {note && <p className="cs-note">{note}</p>}
    </div>
  );
}
