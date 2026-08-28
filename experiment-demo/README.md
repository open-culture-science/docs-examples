# Experiment demo — single-media perfusion

A minimal three-script example against the habitat API: prime a media line,
run timed media exchanges on a chip, then park the rig dry. Meant to be
copied onto a rig and run there against its own habitat instance.

It targets a chip with **one media source, one waste per pump, and four chip
ports** — a high and a low port on each side of a single chamber:

```
dispense pump (pump 0):  air, media source, waste, chip High In,  chip Low In
aspirate pump (pump 1):  air, waste,               chip High Out, chip Low Out
```

If your rig only has a single inflow/outflow per chip, most of
`habitat_chip.py` still applies directly — see [SCRIPTS.md](SCRIPTS.md) for
what's specific to the four-port case.

```
01_prime.py  →  02_experiment.py  →  03_sleep.py
```

## Requirements

- Python 3.10+
- A habitat instance reachable from wherever you run these — normally that
  means running them **on the rig itself**, talking to `127.0.0.1:8000`
- Your rig's chip port-map (which port number is which reagent/chip port)

```bash
pip install -r requirements.txt
```

## Setup

1. **Get the port map.** From the rig:

   ```bash
   curl -s http://127.0.0.1:8000/config/port-map | python3 -m json.tool
   ```

   Find the port numbers for your media source, waste (on each pump), and
   the four chip ports.

2. **Edit `demo.yaml`.** Fill in `habitat.url`, `habitat.chip_id`, and the
   four `session.chip_ports` values from step 1. They ship as `null` on
   purpose — the scripts refuse to start until they're set, rather than
   silently addressing the wrong port on the valve.

3. **Set your token, if your rig enforces auth:**

   ```bash
   export HABITAT_API_TOKEN=hab_user_...
   ```

   If the rig doesn't enforce auth, skip this — the scripts detect that and
   send no auth header at all.

## Run

```bash
python3 01_prime.py          # prime the media line, fill the chamber
python3 02_experiment.py     # run the timed media-exchange cycles
python3 03_sleep.py          # drain, purge, and park the rig dry
```

Every script takes `--config demo.yaml` by default and has its own `--help`.
`02_experiment.py --dry-run` prints the planned schedule without opening a
connection or moving anything — a good first check after editing the YAML.

See [SCRIPTS.md](SCRIPTS.md) for what each script actually does step by
step, every flag, and the caveats worth knowing before a real run (in
particular: metered removal through a *high* outlet only works while the
chamber level is above that port — read the caveat in `02_experiment.py`'s
section before trusting a long run).

## Files

| File | |
|---|---|
| `01_prime.py`, `02_experiment.py`, `03_sleep.py` | The three stages |
| `demo.yaml` | All the numbers — ports, volumes, timing |
| `habitat_chip.py` | The client library the scripts are built on |
| `SCRIPTS.md` | Full per-script reference |
| `requirements.txt` | `httpx`, `pyyaml` |
