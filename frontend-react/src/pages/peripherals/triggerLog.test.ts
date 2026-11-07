import { describe, expect, it } from 'vitest';

import { outcomeOf, outcomesOf } from './TriggerLog';

describe('trigger log outcomes', () => {
  it('says what happened in words, by device name', () => {
    const out = outcomesOf({ _actuation: 'log_only', 'relay-1-ch8-siren-bk9q': 'log_only' },
                           { 'relay-1-ch8-siren-bk9q': 'Relay 1 · CH8 (siren)' });
    expect(out).toEqual([{ device: 'Relay 1 · CH8 (siren)', raw: 'log_only',
                           text: 'Log only — rule not armed, nothing sent', tone: 'muted' }]);
  });

  it('names the protocol and reason of a failure', () => {
    expect(outcomeOf('call_rejected:modbus-tcp/timeout'))
      .toEqual({ text: 'Failed: modbus-tcp/timeout', tone: 'bad' });
  });

  it.each([
    ['called', 'ok'], ['held', 'ok'], ['zone_already_empty', 'muted'],
    ['no_binding', 'warn'], ['call_failed', 'bad'],
  ])('%s is %s', (token, tone) => {
    expect(outcomeOf(token).tone).toBe(tone);
  });

  it('shows an unknown token as itself rather than hiding it', () => {
    expect(outcomeOf('some_new_thing')).toEqual({ text: 'some new thing', tone: 'bad' });
  });
});
