#!/usr/bin/env python3
"""Stage 1: Prime the media line and fill the chip.

Takes the rig from "everything dry" to "chamber holding a working volume of
media, ready to feed".

Workflow:
    1. Prime the media source line — aspirate ``tube_line_volume_ul`` of
       media from the source bottle through pump 0 and push it to waste, so
       the line ends full of fresh media instead of air. Repeat
       ``prime_cycles`` times.
    2. Fill the chip — dispense ``initial_fill_ul`` of media through the
       HIGH inlet, then push an air backpad behind it to clear the
       chip-side line.

The fill goes in through high_in rather than low_in because that is the
inlet the feeds in stage 2 use; filling through it wets the line the rest
of the session depends on. Dropping media into a dry chamber from above can
trap bubbles against the chamber walls — if you see that on this chip,
switch to ``--in-port low_in``.

Session construction homes both pumps first (valve to waste, plunger to 0),
so whatever the previous script left in a syringe does not carry in here.

Run:
    python3 01_prime.py
    python3 01_prime.py --config demo.yaml
    python3 01_prime.py --skip-prime              # line already full of media
    python3 01_prime.py --fill-ul 300             # smaller starting volume
    python3 01_prime.py --in-port low_in          # fill from the bottom instead
    python3 01_prime.py --no-air-backpad          # leave media standing in the line
"""

import argparse

import habitat_chip as hc


REAGENT = "media"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="demo.yaml",
                    help="Path to YAML config (default: demo.yaml)")
    ap.add_argument("--skip-prime", action="store_true",
                    help="Skip priming the media source line (only safe if the "
                         "line is already full of fresh media)")
    ap.add_argument("--skip-fill", action="store_true",
                    help="Skip the chip fill — prime the line and stop")
    ap.add_argument("--cycles", type=int,
                    help="Media prime cycles (default: protocol.prime_cycles)")
    ap.add_argument("--fill-ul", type=float,
                    help="Volume dispensed into the chip (default: protocol.initial_fill_ul)")
    ap.add_argument("--air-backpad-ul", type=float,
                    help="Air pushed after the fill (default: protocol.initial_fill_air_backpad_ul)")
    ap.add_argument("--no-air-backpad", action="store_true",
                    help="Skip the air backpad, leaving media standing in the chip-side line")
    ap.add_argument("--in-port", default="high_in", choices=hc.CHIP_INFLOW_PORT_NAMES,
                    help="Chip inlet to fill through (default: high_in)")
    args = ap.parse_args()

    session, protocol = hc.load_session_from_yaml(args.config)

    cycles = args.cycles if args.cycles is not None else int(protocol.get("prime_cycles", 1))
    fill_ul = (args.fill_ul if args.fill_ul is not None
               else float(protocol.get("initial_fill_ul", session.working_volume_ul)))
    if args.no_air_backpad:
        air_ul = 0.0
    elif args.air_backpad_ul is not None:
        air_ul = args.air_backpad_ul
    else:
        air_ul = float(protocol.get("initial_fill_air_backpad_ul", session.air_backpad_ul))

    in_port = hc.chip_port(session, args.in_port)

    # 1. Media source line: source -> syringe -> waste, so the line ends
    #    holding fresh media rather than the air deprime left behind.
    if not args.skip_prime:
        hc.prime(session, REAGENT, cycles=cycles)

    # 2. Chip fill — reagent then air backpad, out through the chosen inlet.
    if not args.skip_fill:
        hc.fill_chip(session, REAGENT, volume_ul=fill_ul,
                     in_port=in_port, air_backpad_ul=air_ul,
                     label="initial_fill")

    print(f"\nDone. Chip holds ~{fill_ul:.0f}uL media. Next: python3 02_experiment.py")


if __name__ == "__main__":
    main()
