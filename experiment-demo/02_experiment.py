#!/usr/bin/env python3
"""Stage 2: Timed media exchange.

One feed = remove ``feed_volume_ul`` of spent media from the well through
the HIGH outlet, then put the same volume of fresh media back in through the
HIGH inlet. Removal first, so the chamber has headroom before fresh media
arrives. Repeat ``feed_cycles`` times, waiting ``feed_interval_s`` between
feeds.

The well level is the same at the end of each feed as it was at the start —
only the fluid is newer.

Pre-requisites:
    Stage 1 (01_prime.py) has run, so the media line is primed and the
    chamber holds a working volume.

No air backpad on the feeds. On a single-reagent rig there is nothing to
cross-contaminate, so media standing in the chip-side line between feeds
costs nothing and makes the next feed deliver immediately instead of
spending its first stroke re-wetting the line. Pushing air through an inlet
on a near-full chamber is also how bubbles get into the well. Pass
``--air-backpad-ul`` if you want one anyway.

CAVEAT — a metered removal through the HIGH outlet only moves liquid while
the chamber level is above that port. Once the level drops below it, the
pump aspirates air and reports success: the motion is identical either way,
and neither this script nor the pump can tell the difference. The volumes
logged are the volumes requested. Check where high_out actually sits in the
chamber before trusting a long run, or switch to ``--out-port low_out``,
which is exact at any level.

Ctrl-C stops between feeds and closes the event log cleanly; a Ctrl-C
during a motion leaves the pumps wherever they were — run reset-style
homing (any script's session construction) before continuing.

Run:
    python3 02_experiment.py
    python3 02_experiment.py --config demo.yaml
    python3 02_experiment.py --cycles 20 --interval-s 60
    python3 02_experiment.py --volume-ul 200
    python3 02_experiment.py --out-port low_out       # exact removal at any level
    python3 02_experiment.py --dry-run                # print the plan, move nothing
"""

import argparse
import time

import yaml

import habitat_chip as hc


REAGENT = "media"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="demo.yaml",
                    help="Path to YAML config (default: demo.yaml)")
    ap.add_argument("--events", help="Override events_path from YAML")
    ap.add_argument("--cycles", type=int,
                    help="Number of feeds (default: protocol.feed_cycles)")
    ap.add_argument("--volume-ul", type=float,
                    help="Volume exchanged per feed (default: protocol.feed_volume_ul)")
    ap.add_argument("--interval-s", type=float,
                    help="Wait between feeds in seconds (default: protocol.feed_interval_s)")
    ap.add_argument("--air-backpad-ul", type=float, default=0.0,
                    help="Air pushed after each feed's dispense (default 0 — see docstring)")
    ap.add_argument("--out-port", default="high_out", choices=hc.CHIP_OUTFLOW_PORT_NAMES,
                    help="Chip outlet the removal pulls from (default: high_out)")
    ap.add_argument("--in-port", default="high_in", choices=hc.CHIP_INFLOW_PORT_NAMES,
                    help="Chip inlet the replacement goes into (default: high_in)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the feed schedule and exit without connecting or moving anything")
    args = ap.parse_args()

    # Read the YAML directly first so --dry-run can print the plan without
    # opening a connection. Building a Session homes both pumps as a side
    # effect, which is motion — a flag that promises to move nothing has to
    # decide before that happens.
    with open(args.config) as f:
        cfg = yaml.safe_load(f) or {}
    protocol = cfg.get("protocol", {}) or {}
    chip_ports = (cfg.get("session", {}) or {}).get("chip_ports", {}) or {}

    events_path = args.events or protocol.get("events_path", "./events/demo.jsonl")
    cycles = args.cycles if args.cycles is not None else int(protocol.get("feed_cycles", 10))
    volume_ul = (args.volume_ul if args.volume_ul is not None
                 else float(protocol.get("feed_volume_ul", 150.0)))
    interval_s = (args.interval_s if args.interval_s is not None
                  else float(protocol.get("feed_interval_s", 30.0)))

    if cycles < 1:
        raise SystemExit(f"--cycles must be at least 1 (got {cycles})")

    # The last feed is not followed by a wait — the run ends on the feed.
    total_wait_s = interval_s * (cycles - 1)
    print(
        f"{cycles} feed(s) of {volume_ul:.0f}uL {REAGENT}: "
        f"out via {args.out_port} (port {chip_ports.get(args.out_port)}), "
        f"in via {args.in_port} (port {chip_ports.get(args.in_port)}). "
        f"{interval_s:.0f}s between feeds, {total_wait_s / 60:.1f} min of waiting total."
    )
    if args.dry_run:
        print("--dry-run: no connection opened, nothing moved.")
        return

    # From here on we touch hardware. make_session validates chip_ports and
    # homes both pumps, so the syringes start empty whatever ran before.
    session, protocol = hc.load_session_from_yaml(args.config)
    out_port = hc.chip_port(session, args.out_port)
    in_port = hc.chip_port(session, args.in_port)

    hc.begin_recording(session, events_path)
    try:
        for i in range(1, cycles + 1):
            hc.pump_log(f"FEED   cycle {i}/{cycles}")
            hc.exchange_media(
                session,
                REAGENT,
                volume_ul=volume_ul,
                out_port=out_port,
                in_port=in_port,
                air_backpad_ul=args.air_backpad_ul,
                label=f"feed_{i}",
            )
            if i < cycles:
                hc.pump_log(f"FEED   waiting {interval_s:.0f}s before cycle {i + 1}")
                time.sleep(interval_s)
    except KeyboardInterrupt:
        hc.pump_log("FEED   interrupted by operator — closing event log")
    finally:
        hc.end_recording(session)

    print(f"\nDone. Event log: {events_path}")


if __name__ == "__main__":
    main()
