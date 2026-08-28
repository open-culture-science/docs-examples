# Scripts reference — single-media demo

Three scripts, one shared YAML (`demo.yaml`), one library (`habitat_chip.py`).
All numbers live in the YAML; the scripts encode only the protocol shape.

```
01_prime.py  →  02_experiment.py  →  03_sleep.py
```

Typical run:

```bash
python3 01_prime.py          # prime media line, fill chamber to 500 uL
python3 02_experiment.py     # 10 feeds, 150 uL exchanged, 30 s apart
python3 03_sleep.py          # drain, air-purge, deprime — park it dry
```

---

## Auth

If your rig's habitat API enforces auth, every script here needs a user-role
token in the environment:

```bash
export HABITAT_API_TOKEN=hab_user_...
```

`habitat_chip.py` reads it from `HABITAT_API_TOKEN` and sends
`Authorization: Bearer ...` on every request — never from a config file.
Session construction checks `/auth/status` first and exits with a plain
message if the token is missing or rejected, rather than letting the first
401 surface as a traceback partway into a run.

Rigs that do not enforce auth are unaffected: with no token set, no header is
sent and everything works exactly the same.

---

## The rig

One media source, one waste per pump, and a chip with an inlet and an outlet
at each of two heights in a single chamber.

| Pump | Port | Role | Label |
|---|---|---|---|
| 0 (dispense) | 1 | `air` | Air |
| 0 | 8 | `media` | Culture media |
| 0 | 9 | `waste` | Waste |
| 0 | *TODO* | `chip` | High In |
| 0 | *TODO* | `chip` | Low In |
| 1 (aspirate) | 1 | `waste` | Waste |
| 1 | 9 | `air` | Air |
| 1 | *TODO* | `chip` | High Out |
| 1 | *TODO* | `chip` | Low Out |

Inlets must be on pump 0 and outlets on pump 1 — pump 0 is the only one that
can reach the media source, pump 1 the only one that drains.

The four chip ports are named in `demo.yaml` under `session.chip_ports`, and
scripts address them by name (`high_in`, `low_in`, `high_out`, `low_out`),
never by number. The defaults every generic routine uses are **`high_in` for
filling** and **`low_out` for draining** — you drain from the bottom because
that is the only port that can pull the chamber to empty.

**The four port numbers ship as `null` and must be filled in before the first
run.** A null raises at session construction rather than reaching the valve as
a port number.

---

## `01_prime.py` — prime the media line and fill the chip

**Purpose:** take the rig from fully dry to a chamber holding a working volume
of media.

**Workflow:**
1. **Prime the media source line** — aspirate `tube_line_volume_ul` (760 uL)
   of media from the bottle through pump 0 and push it to waste, so the line
   ends full of fresh media instead of the air `03_sleep.py` left in it.
2. **Fill the chip** — dispense `initial_fill_ul` (500 uL) through `high_in`,
   then push a 200 uL air backpad behind it to clear the chip-side line.
   Reagent and air are drawn in one load, so the air follows the media out.

The fill goes through `high_in` because that is the inlet the feeds use —
filling through it wets the line the rest of the session depends on. Dropping
500 uL into a dry chamber from above can trap bubbles against the walls; if
you see that, use `--in-port low_in`.

**Flags:**

| Flag | Default | Effect |
|---|---|---|
| `--config PATH` | `demo.yaml` | YAML config path |
| `--skip-prime` | off | Skip priming the source line (only if it's already full of fresh media) |
| `--skip-fill` | off | Prime the line and stop |
| `--cycles N` | `protocol.prime_cycles` (1) | Tube-line volumes pushed to waste |
| `--fill-ul N` | `protocol.initial_fill_ul` (500) | Volume dispensed into the chip |
| `--air-backpad-ul N` | `protocol.initial_fill_air_backpad_ul` (200) | Air pushed after the fill |
| `--no-air-backpad` | off | Leave media standing in the chip-side line |
| `--in-port NAME` | `high_in` | Inlet to fill through (`high_in` / `low_in`) |

```bash
python3 01_prime.py
python3 01_prime.py --skip-prime            # line already full of media
python3 01_prime.py --fill-ul 300           # smaller starting volume
python3 01_prime.py --in-port low_in        # fill from the bottom
```

---

## `02_experiment.py` — timed media exchange

**Purpose:** keep the chamber's media fresh on a fixed interval.

**Workflow:** one feed = remove `feed_volume_ul` (150 uL) through `high_out`,
then put the same volume of fresh media back through `high_in`. Removal comes
first, so the chamber has headroom before fresh media arrives. Repeat
`feed_cycles` times with `feed_interval_s` between feeds. The final feed is
not followed by a wait.

The well level ends each feed where it started — only the fluid is newer.

**No air backpad on the feeds.** There is one reagent on this rig, so nothing
can cross-contaminate, and media standing in the chip-side line between feeds
means the next feed delivers immediately instead of spending its first stroke
re-wetting the line. Pushing air through an inlet on a near-full chamber is
also how bubbles get into the well. `--air-backpad-ul` overrides.

**No over-aspirate compensation on the removal.** `aspirate_overshoot_ul`
deliberately pulls extra to guarantee an empty well — right for a drain, wrong
when the goal is "remove exactly 150 uL". It stays off here and on in
`03_sleep.py`'s drain.

> **Caveat — `high_out` removal is level-dependent.** A metered pull through
> the high outlet only moves liquid while the chamber level is above that
> port. Below it, the pump aspirates air and reports success: the motion is
> identical either way, and neither the script nor the pump can tell the
> difference. The volumes in the log are the volumes *requested*. Check where
> `high_out` actually sits before trusting a long run, or use
> `--out-port low_out`, which is exact at any level.

**Flags:**

| Flag | Default | Effect |
|---|---|---|
| `--config PATH` | `demo.yaml` | YAML config path |
| `--events PATH` | `protocol.events_path` | Override the event log path |
| `--cycles N` | `protocol.feed_cycles` (10) | Number of feeds |
| `--volume-ul N` | `protocol.feed_volume_ul` (150) | Volume exchanged per feed |
| `--interval-s N` | `protocol.feed_interval_s` (30) | Wait between feeds |
| `--air-backpad-ul N` | `0` | Air pushed after each feed's dispense |
| `--out-port NAME` | `high_out` | Outlet the removal pulls from |
| `--in-port NAME` | `high_in` | Inlet the replacement goes into |
| `--dry-run` | off | Print the schedule and exit — opens no connection, moves nothing |

```bash
python3 02_experiment.py
python3 02_experiment.py --cycles 20 --interval-s 60
python3 02_experiment.py --out-port low_out       # exact removal at any level
python3 02_experiment.py --dry-run                # check the plan first
```

**Output:** `events/demo.jsonl`, one JSON object per feed (`feed_1`, `feed_2`,
…) plus an enclosing `recording` record, each with start/end relative seconds,
duration, and absolute timestamps. Flushed after every feed, so it reflects
what actually happened if the run is interrupted.

**Ctrl-C** between feeds stops cleanly and closes the event log. Ctrl-C
*during* a motion leaves the pumps mid-stroke — the next script's session
construction homes them, so just run the next stage.

---

## `03_sleep.py` — park the rig dry

**Purpose:** end state where every line on both pumps is air-filled and the
chamber is empty. Safe to disconnect the chip or the media bottle.

**Workflow:**
1. **Drain the well** — sweep both outlets, high first. The high port takes
   off everything above its height; the low port, going last, pulls the
   chamber down to empty. Default volume (700 uL) is more than one working
   volume on purpose, so the sweep runs past dry rather than stopping short.
   The surplus is air and goes to waste like everything else.
2. **Air-purge all four chip lines** — pump 0 pushes an air slug out through
   each inlet while pump 1 draws through the low outlet in 100 uL chunks
   (Habitat runs one motion at a time; pushing a whole slug in before drawing
   any off would pressurise the well). Then pump 1 clears each outlet line to
   waste.
3. **De-prime media and waste on both pumps** — push air back up each line so
   residual media returns to its bottle. `deprime_all_lines` always skips
   chip- and air-role ports, so this hits exactly the media source and the two
   waste lines.

The purge runs *after* the drain. Sweeping air through a full chamber blows
bubbles into it — the same reason `flush_chip` defers its air purge until all
fill/drain cycles are done.

> **This parks the rig dry. It does not sterilize and does not keep a sample
> alive.** The reference rig's sleep stage fills the chip with H2O2 for
> storage; there is no H2O2 on this setup. If a sample needs to stay wet, stop
> after `02_experiment.py` instead of running this.

**Flags:**

| Flag | Default | Effect |
|---|---|---|
| `--config PATH` | `demo.yaml` | YAML config path |
| `--skip-drain` | off | Skip draining (well known empty) |
| `--skip-purge` | off | Leave the chip lines wet |
| `--skip-deprime` | off | Leave source/waste lines primed |
| `--drain-ul N` | `protocol.sleep_drain_volume_ul` (700) | Volume pulled through **each** outlet |
| `--purge-air-ul N` | `protocol.sleep_purge_air_ul` (300) | Air pushed through each chip line |

```bash
python3 03_sleep.py
python3 03_sleep.py --skip-drain
python3 03_sleep.py --drain-ul 900 --purge-air-ul 400
```

---

## Four-port chip support in `habitat_chip.py`

`habitat_chip.py` is a general-purpose client library for a habitat rig —
priming lines, filling/draining a chip, timed dosing, cleanup — built around
a single named inflow and outflow per chip (`chip_inflow_port` /
`chip_outflow_port`, still what `dose` / `feed` / `swap_chip` reach for). On
top of that, it adds explicit support for a chip with two heights per side:

| Function | What it does |
|---|---|
| `Session.chip_ports` | `{high_in, low_in, high_out, low_out}` from YAML. Validated at session construction: unknown key or null port raises before any motion. |
| `chip_port(session, name)` | Resolve a name to a port number. Raises on a typo rather than guessing. |
| `chip_inflow_ports` / `chip_outflow_ports` | The ports as a list. Outflows come back **high first**, so a sweep finishes on the low port and reaches empty. Falls back to autodiscovery (`role="chip"`) on rigs that don't set `chip_ports`. |
| `drain_chip(outflow_ports=[...])` | Sweep several outlets instead of one. |
| `purge_chip_ports(session)` | Air through all four chip lines, both pumps. `dry_chip` only does one in / one out. |
| `fill_chip(session, ...)` | Dispense into the chamber through one inlet, optional air backpad. Adds only — use on an empty chamber. |
| `exchange_media(session, ...)` | Remove N uL through an outlet, put N uL of fresh reagent back through an inlet. No syringe wash, no backpad, no overshoot by default. |
| `flush_chip(outflow_port=...)` | Was hardcoded to `session.chip_outflow_port`; now overridable. Still drains through one port per cycle by design. |

There is no separate recovery script here. Session construction homes both
pumps (valve to waste, plunger to 0) automatically, which covers the common
"previous script left a syringe part-full" case; a stalled valve needs
manual intervention through the habitat GUI or API directly.
