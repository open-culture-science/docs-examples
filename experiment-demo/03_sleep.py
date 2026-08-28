#!/usr/bin/env python3
"""Stage 3: Shut the rig down dry.

Workflow:
    1. Drain the well — sweep BOTH chip outlets, high port first. The high
       port takes off everything above its height; the low port, going last,
       pulls the chamber down to empty. Drain volume defaults to more than
       one working volume so the sweep runs past dry rather than stopping
       short; the surplus is air and goes to waste like everything else.
    2. Air-purge all four chip lines — pump 0 pushes an air slug out through
       each inlet while pump 1 draws through the low outlet, then pump 1
       clears each outlet line to waste. Runs on an already-drained well, so
       no bubbles get blown into standing liquid.
    3. De-prime the media and waste lines on both pumps — push air back up
       each line so residual media returns to its bottle and every non-chip
       line ends air-filled. Safe to disconnect the chip or the media bottle
       afterwards.

End state: both syringes empty, chamber empty, every line on both pumps
air-filled.

This parks the rig DRY. It does not sterilize anything and does not keep a
sample alive — the reference rig's sleep stage fills the chip with H2O2 for
storage, and there is no H2O2 on this setup. If a sample needs to stay wet,
stop after stage 2 rather than running this.

Run:
    python3 03_sleep.py
    python3 03_sleep.py --config demo.yaml
    python3 03_sleep.py --skip-drain             # well already empty
    python3 03_sleep.py --skip-purge             # leave chip lines wet
    python3 03_sleep.py --skip-deprime           # leave source/waste lines primed
    python3 03_sleep.py --drain-ul 900 --purge-air-ul 400
"""

import argparse

import habitat_chip as hc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="demo.yaml",
                    help="Path to YAML config (default: demo.yaml)")
    ap.add_argument("--skip-drain", action="store_true",
                    help="Skip draining the well (use when it is known empty)")
    ap.add_argument("--skip-purge", action="store_true",
                    help="Skip the air purge of the four chip lines")
    ap.add_argument("--skip-deprime", action="store_true",
                    help="Skip de-priming the media + waste lines on both pumps")
    ap.add_argument("--drain-ul", type=float,
                    help="Volume pulled through EACH outlet (default: protocol.sleep_drain_volume_ul)")
    ap.add_argument("--purge-air-ul", type=float,
                    help="Air pushed through each chip line (default: protocol.sleep_purge_air_ul)")
    args = ap.parse_args()

    session, protocol = hc.load_session_from_yaml(args.config)

    drain_ul = (args.drain_ul if args.drain_ul is not None
                else float(protocol.get("sleep_drain_volume_ul",
                                        session.working_volume_ul * 1.4)))
    purge_air_ul = (args.purge_air_ul if args.purge_air_ul is not None
                    else float(protocol.get("sleep_purge_air_ul", 300.0)))

    outflows = hc.chip_outflow_ports(session)
    inflows = hc.chip_inflow_ports(session)

    # 1. Empty the chamber through both outlets, finishing on the low one.
    if not args.skip_drain:
        hc.pump_log(f"SLEEP  drain well {drain_ul:.0f}uL via outlets {outflows}")
        hc.drain_chip(session, drain_volume_ul=drain_ul, outflow_ports=outflows)

    # 2. Air through every chip line. Must come after the drain — sweeping
    #    air through a full chamber blows bubbles into it.
    if not args.skip_purge:
        hc.pump_log(f"SLEEP  purge chip lines: inlets {inflows}, outlets {outflows}")
        hc.purge_chip_ports(session, air_volume_ul=purge_air_ul,
                            inflow_ports=inflows, outflow_ports=outflows)

    # 3. Media + waste lines on both pumps end air-filled. deprime_all_lines
    #    always skips chip- and air-role ports, so this is exactly the media
    #    source and the two waste lines.
    if not args.skip_deprime:
        hc.pump_log("SLEEP  deprime non-chip lines on pump 0, then pump 1")
        hc._run_serial([
            lambda: hc.deprime_all_lines(session, hc.DISPENSE_PUMP_ADDR),
            lambda: hc.deprime_all_lines(session, hc.ASPIRATE_PUMP_ADDR),
        ])

    print("\nDone. Chamber empty, all lines air-filled. Safe to disconnect.")


if __name__ == "__main__":
    main()
