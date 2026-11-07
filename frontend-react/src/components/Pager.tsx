/**
 * Pager — "21–40 of 312 · ← Prev · Next →" for any offset/limit list (PTZ presets, position history, patrol
 * run log). Zero-based page index; renders nothing when everything fits on one page.
 */
export function Pager({ page, size, total, onPage, unit = '' }: {
  page: number; size: number; total: number; onPage: (p: number) => void; unit?: string;
}) {
  if (total <= size) return total ? <div className="pager-note">{total} {unit}</div> : null;
  const from = page * size + 1;
  const to = Math.min(total, (page + 1) * size);
  const last = Math.max(0, Math.ceil(total / size) - 1);
  return (
    <div className="pager">
      <span className="pager-note">{from}–{to} of {total} {unit}</span>
      <button className="btn-subtle btn-sm" disabled={page === 0} onClick={() => onPage(0)} title="First page">«</button>
      <button className="btn-subtle btn-sm" disabled={page === 0} onClick={() => onPage(page - 1)}>← Prev</button>
      <button className="btn-subtle btn-sm" disabled={page >= last} onClick={() => onPage(page + 1)}>Next →</button>
      <button className="btn-subtle btn-sm" disabled={page >= last} onClick={() => onPage(last)} title="Last page">»</button>
    </div>
  );
}
