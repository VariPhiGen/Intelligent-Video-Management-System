/**
 * whep.ts — a minimal WHEP (WebRTC-HTTP Egress Protocol) client for mediamtx.
 *
 * WHEP is the standard "play a WebRTC stream over one HTTP request" handshake,
 * and mediamtx serves it at http://<host>:8889/<path>/whep. The flow is
 * deliberately non-trickle — gather ICE, POST the full offer, apply the answer
 * — because on the operator LAN gathering finishes in a few milliseconds and
 * non-trickle needs no PATCH/candidate endpoint, so it is the smaller correct
 * thing.
 *
 * No STUN/TURN: mediamtx is host-networked and the operator is on the same LAN,
 * so the only ICE candidate that matters is the host address, reachable
 * directly. Adding public STUN here would just be round trips to the internet
 * on an on-prem box.
 *
 * The caller owns the <video>; this attaches a MediaStream to it and resolves
 * once the peer connection reaches `connected`. Any failure — HTTP error, SDP
 * problem, ICE never connecting within the timeout — rejects, and the caller
 * falls back to HLS. `close()` tears down the peer and tells mediamtx to drop
 * the session (DELETE on the resource URL) so a re-attach does not leak a
 * reader on the relay.
 */

const ICE_GATHER_TIMEOUT_MS = 700;   // LAN gathering is instant; this is a ceiling
const CONNECT_TIMEOUT_MS = 4000;     // no connection by here → fall back to HLS

export interface WhepSession {
  pc: RTCPeerConnection;
  close: () => void;
}

function waitIceGathering(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise(resolve => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === 'complete') done(); };
    pc.addEventListener('icegatheringstatechange', check);
    // Non-trickle would otherwise wait for a candidate that never comes on a
    // host with one interface — cap it and send whatever we have.
    setTimeout(done, ICE_GATHER_TIMEOUT_MS);
  });
}

export async function attachWhep(video: HTMLVideoElement, url: string): Promise<WhepSession> {
  const pc = new RTCPeerConnection({ iceServers: [] });
  let resourceUrl: string | null = null;
  let settled = false;

  const close = () => {
    try { pc.close(); } catch { /* already gone */ }
    if (video.srcObject) video.srcObject = null;
    // Best-effort: let mediamtx release the reader immediately rather than on
    // its own ICE timeout. Fire-and-forget; the page is moving on regardless.
    if (resourceUrl) {
      fetch(resourceUrl, { method: 'DELETE' }).catch(() => { /* nothing to do */ });
    }
  };

  // Receive-only: the camera sends, we never send. Audio transceiver too —
  // mediamtx simply omits it when the camera has none, and asking costs nothing.
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.addTransceiver('audio', { direction: 'recvonly' });

  // Ask the browser for the SMALLEST safe jitter buffer. WebRTC defaults to a
  // comfortable playout delay (padding against packet reordering) that on a
  // clean LAN is pure latency — it is most of the ~1 s left after WebRTC is
  // working. playoutDelayHint is the old Chrome lever (seconds), jitterBuffer
  // Target the standardised one (ms); set both, the browser uses whichever it
  // has. 0 means "as low as you can", not a hard zero — the browser still
  // grows the buffer if the network actually needs it, so this trades away
  // latency, not resilience.
  const minimiseLatency = () => {
    for (const r of pc.getReceivers()) {
      try { (r as any).playoutDelayHint = 0; } catch { /* not supported */ }
      try {
        if ('jitterBufferTarget' in r) (r as unknown as { jitterBufferTarget: number }).jitterBufferTarget = 0;
      } catch { /* not supported */ }
    }
  };

  const stream = new MediaStream();
  pc.addEventListener('track', e => {
    stream.addTrack(e.track);
    if (video.srcObject !== stream) video.srcObject = stream;
    minimiseLatency();
  });

  const connected = new Promise<void>((resolve, reject) => {
    const t = setTimeout(() => {
      if (!settled) { settled = true; reject(new Error('whep connect timeout')); }
    }, CONNECT_TIMEOUT_MS);
    pc.addEventListener('connectionstatechange', () => {
      if (settled) return;
      if (pc.connectionState === 'connected') {
        settled = true; clearTimeout(t); resolve();
      } else if (pc.connectionState === 'failed') {
        settled = true; clearTimeout(t); reject(new Error('whep ice failed'));
      }
    });
  });

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIceGathering(pc);

    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp' },
      body: pc.localDescription?.sdp ?? offer.sdp ?? '',
    });
    if (!res.ok) throw new Error(`whep http ${res.status}`);

    const loc = res.headers.get('Location');
    if (loc) { try { resourceUrl = new URL(loc, url).href; } catch { resourceUrl = null; } }

    const answer = await res.text();
    await pc.setRemoteDescription({ type: 'answer', sdp: answer });

    await connected;
    minimiseLatency();
    return { pc, close };
  } catch (err) {
    close();
    throw err;
  }
}
