/**
 * StqcBadge.tsx — one camera's STQC/BIS-ER posture, at a glance.
 *
 * Colour carries the compliance reading, not the sentiment: 'unverified' is
 * amber rather than grey because an unchecked camera is an open question the
 * operator owns, and rendering it as neutral is how a fleet quietly stays
 * unverified. Only 'certified' is green, and only until its certificate is
 * inside the expiry window — an expired certificate is not a certificate.
 */
import type { Camera, StqcStatus } from '@/lib/types';
import { STQC_LABELS, STQC_EXPIRY_WARN_DAYS } from '@/lib/types';

type Tone = 'ok' | 'warn' | 'bad' | 'mute';

// The semantic ramp already ships matched fg/bg pairs at equal chroma, so the
// four tones read as one family rather than four separate decisions.
const TONE: Record<Tone, { fg: string; bg: string }> = {
  ok:   { fg: 'var(--green)',  bg: 'var(--greenSoft)' },
  warn: { fg: 'var(--yellow)', bg: 'var(--yellowSoft)' },
  bad:  { fg: 'var(--red)',    bg: 'var(--redSoft)' },
  mute: { fg: 'var(--muted)',  bg: 'var(--hover)' },
};

/** Whole days from today to `iso` (negative once past). Date-only arithmetic:
 *  certificates carry calendar validity, so a timezone-sensitive comparison
 *  would flip the badge a few hours early or late for no reason. */
export function daysUntil(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number);
  const then = Date.UTC(y, (m || 1) - 1, d || 1);
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((then - today) / 86_400_000);
}

/** The posture actually worth displaying, which is not always the stored one:
 *  a 'certified' camera whose certificate has lapsed reads as expired. */
export function stqcDisplay(
  status: StqcStatus,
  validUntil: string | null,
): { label: string; tone: Tone; detail: string } {
  if (status === 'certified' && validUntil) {
    const left = daysUntil(validUntil);
    if (left < 0) return { label: 'Certificate expired', tone: 'bad', detail: `Expired ${validUntil}` };
    if (left <= STQC_EXPIRY_WARN_DAYS) {
      return { label: 'Expiring soon', tone: 'warn', detail: `${left} day${left === 1 ? '' : 's'} left` };
    }
    return { label: STQC_LABELS.certified, tone: 'ok', detail: `Valid to ${validUntil}` };
  }
  switch (status) {
    case 'certified':      return { label: STQC_LABELS.certified, tone: 'ok', detail: 'No expiry recorded' };
    case 'not_certified':  return { label: STQC_LABELS.not_certified, tone: 'bad', detail: 'Checked — not certified' };
    case 'exempt':         return { label: STQC_LABELS.exempt, tone: 'mute', detail: 'Outside the mandate' };
    case 'not_applicable': return { label: STQC_LABELS.not_applicable, tone: 'mute', detail: 'Not procured in India' };
    default:               return { label: STQC_LABELS.unknown, tone: 'warn', detail: 'Nobody has checked this' };
  }
}

export function StqcBadge({ camera, showDetail = false }: {
  camera: Pick<Camera, 'stqc_status' | 'stqc_valid_until'>;
  showDetail?: boolean;
}) {
  const { label, tone, detail } = stqcDisplay(camera.stqc_status || 'unknown', camera.stqc_valid_until);
  const t = TONE[tone];
  return (
    <span
      title={detail}
      style={{
        display: 'inline-flex', alignItems: 'baseline', gap: 6,
        padding: '2px 8px', borderRadius: 999, background: t.bg, color: t.fg,
        fontSize: 11, fontWeight: 600, whiteSpace: 'nowrap',
      }}
    >
      {label}
      {showDetail && (
        <span style={{ fontWeight: 400, opacity: 0.8, fontFamily: 'var(--mono)', fontSize: 10 }}>
          {detail}
        </span>
      )}
    </span>
  );
}
