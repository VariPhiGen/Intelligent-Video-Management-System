import { describe, expect, it } from 'vitest';
import { toApiError } from './api';

describe('a 422 says what was wrong', () => {
  it('turns FastAPI validation details into a sentence, not "Request failed (HTTP 422)"', async () => {
    const resp = new Response(JSON.stringify({ detail: [
      { loc: ['body', 'timezone'], msg: "Value error, unknown timezone 'Asia/Calcutta' (use an IANA name such as Asia/Kolkata)" },
    ] }), { status: 422 });
    const err = await toApiError(resp);
    expect(err.message).toBe("timezone: unknown timezone 'Asia/Calcutta' (use an IANA name such as Asia/Kolkata)");
  });
});
