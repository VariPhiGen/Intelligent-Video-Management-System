#!/usr/bin/env python3
"""restart_recovery — does a consumer survive the broker restarting?

THE BUG THIS EXISTS TO CATCH, which was live on this appliance and invisible:
restart vms_frames and SmartSearch delivered ZERO frames afterwards, forever,
while /health cheerfully reported five rings mapped and logged nothing. The
shm PATH survives a restart; the file behind it does not. A consumer holding
the previous mapping reads a frozen image whose sequence never advances, so
every read is refused — and a refused read is indistinguishable from ordinary
back-pressure, which is why nothing complained.

The first fix was wrong in an instructive way. It identified each ring by its
INODE, on the reasoning that the inode changes when the file is recreated. It
does not: the ring unlinks its file on close and /dev/shm is a tmpfs, so the
five recreated files came back with the SAME five inode numbers — epochs 2..6
before the restart and 2..6 after. Nothing remapped and the fix measured as a
complete no-op. The epoch is now the ring's creation time.

WHAT THIS RUNS. The SHIPPED consumer (smartsearch/index/frame_source.py), not
a copy of it, with only the motion gate neutralised so every notice attempts a
read — a still scene gates out every frame and would otherwise prove nothing.
Restart the broker while this is running:

    docker restart vms_frames

    python3 scripts/restart_recovery.py --camera cam1-h5e0 --seconds 120
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "smartsearch"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Consumer survival across a restart")
    ap.add_argument("--camera", default="cam1-h5e0")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--redis", default=os.environ.get("REDIS_URL",
                                                      "redis://redis:6379/0"))
    args = ap.parse_args()

    from index.frame_source import BrokerSubscriber

    # THE ONLY THING CHANGED. Every notice attempts a read, so a still scene
    # still exercises the mapping. Nothing else about the consumer differs.
    BrokerSubscriber._gated_out = staticmethod(lambda motion: False)

    got: list[int] = []
    sub = BrokerSubscriber(args.redis,
                           on_frame=lambda s, f, t, m: got.append(int(f[0, 0, 0])))
    sub.want(args.camera)
    sub.start()

    print(f"watching {args.camera} for {args.seconds:.0f}s — "
          f"restart the broker now:  docker restart vms_frames\n")
    print(f"{'t':>5}  {'delivered':>10} {'refused':>9} {'rings':>6}  note")
    start = time.time()
    prev_d = prev_r = 0
    recovered = False
    broke = False
    seen_any = False
    while time.time() - start < args.seconds:
        time.sleep(5)
        s = sub.snapshot()
        d, r = s["frames_delivered"], s["frames_refused"]
        dd, dr = d - prev_d, r - prev_r
        note = ""
        # TWO SIGNATURES OF THE SAME INTERRUPTION, and which one appears is
        # itself diagnostic:
        #   refusals  — the consumer is reading a ring that no longer
        #               advances. It kept a dead mapping. This is the failure.
        #   a stall   — nothing arriving at all, because the broker is down
        #               and re-registering its cameras. Expected, and it ends.
        if dr and not dd:
            note = "REFUSING — stale mapping (the failure)"
            broke = True
        elif not dd and not dr and seen_any:
            note = "stalled — broker down"
            broke = True
        elif dd and broke and not recovered:
            note = "RECOVERED"
            recovered = True
        elif dd:
            note = "delivering"
            seen_any = True
        print(f"{time.time()-start:5.0f}  {d:>10} {r:>9} {s['rings_mapped']:>6}  {note}")
        prev_d, prev_r = d, r
    sub.stop()

    print()
    if not broke:
        print("no interruption seen — restart the broker DURING the window "
              "for this to prove anything")
        return 1
    if recovered:
        print("RESTART RECOVERY: PASS — delivery was interrupted and resumed "
              "without restarting the consumer")
        return 0
    print("RESTART RECOVERY: FAIL — the consumer never recovered, which is "
          "exactly the silent blindness this checks for")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
