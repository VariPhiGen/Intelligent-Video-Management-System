/**
 * TriggerLog — where fired VMS→peripheral triggers will be recorded.
 *
 * It is empty, and that is the accurate state. This tab used to render six
 * hardcoded rows — invented timestamps, invented events, and a named operator
 * ("R. Kulkarni") who unlocked a gate that never existed — under the heading
 * "logged for audit". A fabricated audit trail is the most dangerous kind of
 * demo data: it is indistinguishable from evidence, and it sat next to a CSV
 * export button that would hand it to someone as a file.
 *
 * Nothing can fire a peripheral (no Home-Assistant bridge, no rule engine), so
 * nothing can be logged. When the executor lands it writes rows to a real
 * table and this component reads them.
 */
import { usePeripherals } from './peripheralsData';

export function TriggerLog({ data }: { data: ReturnType<typeof usePeripherals> }) {
  const { rules } = data;

  return (
    <div className="fade">
      <div className="pk-head">
        <div className="pk-head-l" style={{ color: 'var(--muted)', fontSize: 13 }}>
          Where fired VMS→peripheral triggers will be recorded
        </div>
      </div>

      <div className="panel">
        <div className="panel-body">
          <div className="emptystate" style={{ border: 'none' }}>
            <div className="glyph">⎋</div>
            <h4>No triggers recorded</h4>
            <p>
              Nothing has fired a peripheral, because nothing can yet: peripheral
              control needs the Home Assistant bridge and the rule engine, and
              neither is running on this appliance.
              {rules.length > 0 && (
                <> The {rules.length} rule{rules.length === 1 ? '' : 's'} you built
                   {rules.length === 1 ? ' is' : ' are'} held for this session only
                   and will not fire.</>
              )}
            </p>
            <p style={{ fontSize: 11.5, color: 'var(--dim)', marginTop: 8 }}>
              Operator actions on the VMS itself — camera edits, exports, searches —
              are logged today under Administration → Audit log.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
