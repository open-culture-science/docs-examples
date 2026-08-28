"""Python client library for a Habitat-controlled microfluidic rig.

A composition layer on top of Habitat's atomic HTTP API (``POST
/atomics/<name>``) that turns individual valve/plunger commands into
higher-level routines: priming a reagent line, filling or draining a chip
well, exchanging media on a timer, cleaning between reagents, and so on.

Design notes:

- Every routine takes a ``Session`` as its first argument. The session
  holds connection info, the live port-map, the reagent registry, the
  last reagent used (for auto-wash), and default speeds/volumes.
- All execution polls ``is_ready`` between motion commands, so callers
  never need to guess whether a move has actually finished before
  issuing the next one.
- The dual-pump pattern used throughout — drain on the aspirate pump
  while the dispense pump prepares the next delivery — is the main
  building block for anything that needs to move fast between chip
  states (see ``chip_swap``).

Reagent registry can be loaded:
- Explicitly: ``reagents={"my_drug": Reagent(..., port=4, role="drug"), ...}``
- From the live Habitat port-map: ``reagents=None`` -> use labels.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx


def _wait_for_enter(prompt: str) -> None:
    """Block on stdin until the user presses Enter.

    Flushes any pre-buffered bytes on stdin first (a stray newline from
    typing ahead during a previous prompt, copy-paste, or terminal flow
    control) so the prompt cannot be satisfied by stale input. Falls back
    to plain ``input()`` if the stdin descriptor isn't a TTY (e.g., piped
    or no terminal attached).
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError):
        pass  # non-POSIX or no tty — just fall through to readline()
    try:
        sys.stdin.readline()
    except EOFError:
        pump_log("WARN: stdin EOF on user prompt — proceeding without confirmation")


# =============================================================================
# Hardware constants (CENTRIS-V2 pump, 2.5 mL syringe, 181 490 inc/stroke)
# =============================================================================

# Full-stroke encoder count. Fixed by the pump, independent of the syringe
# fitted to it — a 1000 uL syringe still travels 181 490 increments end to
# end, it just moves less liquid per increment.
STROKE_INCREMENTS = 181490
DEFAULT_SYRINGE_VOLUME_UL = 2500.0

# Increments per microlitre. Derived from the syringe actually configured on
# the pump: ``make_session`` overwrites this from the server's reported
# ``syringe_volume_ul`` so a rig fitted with a different syringe converts
# correctly without anyone hand-editing this file. The default matches the
# 2500 uL syringe.
INC_PER_UL = STROKE_INCREMENTS / DEFAULT_SYRINGE_VOLUME_UL  # ~72.596 inc/uL


def _sync_inc_per_ul(url: str) -> float:
    """Set INC_PER_UL from the syringe volume the server reports.

    Every volume->increment conversion in this module goes through
    ``_ul_to_inc``, which reads the module-level ``INC_PER_UL``. Hardcoding
    it to the 2500 uL syringe meant a rig with a different syringe silently
    moved the wrong volume unless someone remembered to edit the constant —
    standards.yaml carries a "CRITICAL, edit hc.INC_PER_UL first" note for
    exactly that reason. Deriving it removes the foot-gun.

    Both pumps must agree: a single module-level factor cannot describe two
    different syringes, so a mismatch warns and keeps the current value.
    """
    global INC_PER_UL
    try:
        pumps = _get_json(url, "/pumps/", timeout_s=10.0)
    except Exception as e:
        pump_log(f"warning: could not read syringe volume ({e!r}) — keeping INC_PER_UL={INC_PER_UL:.3f}")
        return INC_PER_UL

    volumes = {p["addr"]: p.get("syringe_volume_ul") for p in pumps if p.get("syringe_volume_ul")}
    distinct = set(volumes.values())
    if not distinct:
        return INC_PER_UL
    if len(distinct) > 1:
        pump_log(
            f"WARNING: pumps report different syringe volumes {volumes} — a single "
            f"INC_PER_UL cannot be right for both; keeping {INC_PER_UL:.3f} "
            f"(volumes on at least one pump will be wrong)"
        )
        return INC_PER_UL

    volume_ul = float(distinct.pop())
    derived = STROKE_INCREMENTS / volume_ul
    if abs(derived - INC_PER_UL) > 1e-9:
        pump_log(
            f"INC_PER_UL {INC_PER_UL:.3f} -> {derived:.3f} "
            f"({STROKE_INCREMENTS} inc / {volume_ul:.0f}uL syringe, per the server)"
        )
    INC_PER_UL = derived
    return INC_PER_UL

DISPENSE_PUMP_ADDR = 0
ASPIRATE_PUMP_ADDR = 1

# Chip-side valve ports — these are just starting defaults. Override in
# the Session constructor (via YAML or kwargs) to match your own rig's
# wiring.
DEFAULT_CHIP_INFLOW_PORT = 4
DEFAULT_CHIP_OUTFLOW_PORT = 6

# is_ready polling. Motion atomics now block until the job reaches a terminal
# state (``wait_for_motion`` defaults true server-side), so the first poll
# normally returns True immediately and this loop is just a cheap
# post-condition check that no fault got latched. The interval only matters
# if the server ever hands back an early ACK instead of blocking on motion.
READY_POLL_INTERVAL_S = 1.0
# Timeout per motion: large source-side aspirates at slow speed codes
# (e.g., 912 uL air at speed_code=26 ~ 9.6 uL/s) can run ~95 s. We set 180 s
# to give comfortable headroom, while still bounding pathological hangs.
READY_TIMEOUT_S = 180.0

# 429 retry: Habitat returns 429 with body
#   {"retry_after_s": <n>, "limit": <n>, ...}
# when it decides the window is full. The policy it reports back is NOT
# stable — a single dry run on 2026-08-25 saw it claim 60/4s, 60/5s, 300/6s,
# 300/2s and 300/1s on successive hits, and we tripped a "300 per 1s" limit
# while issuing roughly 3 req/s. So don't tune against a specific number:
# honor whatever retry_after_s comes back (capped), and retry.
RATE_LIMIT_MAX_RETRIES = 10
RATE_LIMIT_BASE_BACKOFF_S = 0.5
RATE_LIMIT_MAX_BACKOFF_S = 5.0
# The server's sliding window is 60 s, so an honest Retry-After can be that
# large. Capping the *hint* at 5 s meant 10 retries covered only 50 s and a
# fully drained bucket could outlast us — the call then raised mid-run.
# Honor the hint up to just past the window; the blind exponential fallback
# keeps the small cap, since a guessed wait shouldn't stall the rig a minute.
RATE_LIMIT_MAX_HINT_S = 65.0

# Habitat runs ONE externally-submitted job at a time. A second atomic POST
# issued while another is still running is rejected with
#   409 {"type": ".../rig-busy", "title": "RigBusy", ...}
# rather than queued. Two guards:
#   1. _ATOMIC_LOCK serializes every submission from this process, so our own
#      background threads (dose(prep_next=...) prep thread) can never race.
#   2. 409 rig-busy is retried with backoff, for the case where something
#      ELSE holds the rig (scheduler, HITL console, a second script).
_ATOMIC_LOCK = threading.Lock()
RIG_BUSY_MAX_RETRIES = 20
RIG_BUSY_BASE_BACKOFF_S = 1.0
RIG_BUSY_MAX_BACKOFF_S = 5.0

# Air pushed through the chip is swept in chunks this size, alternating the
# push (pump 0) with the draw (pump 1). These two used to move simultaneously;
# Habitat can no longer run them at once, and shoving a whole air slug in
# before anything is drawn off would pressurize the well.
AIR_SWEEP_CHUNK_UL = 100.0


# =============================================================================
# Reagent + Session data model
# =============================================================================


@dataclass
class Reagent:
    """A named source on the dispense pump's distribution valve."""

    name: str
    port: int                # source port on dispense pump (addr 0)
    role: str = "reagent"    # "media" | "drug" | "wash" | "buffer" | "reagent" | etc.
    label: str = ""          # human-readable; falls back to name


@dataclass
class Session:
    """Mutable execution context for an experiment on one chip.

    Holds connection info, port-map + reagent registry, last-source state
    for auto-wash, defaults for speeds/volumes, and the optional event log.
    """

    url: str
    chip_id: str
    reagents: dict[str, Reagent]
    pump_map: dict[int, dict[int, dict]]

    # Working defaults (override per-call as needed)
    working_volume_ul: float = 100.0
    air_backpad_ul: float = 200.0
    slow_speed_code: int = 26     # chip-facing motions
    offchip_speed_code: int = 17  # off-chip motions
    dry_pause_s: float = 0.0

    # Compensates for a mechanical reverse-creep in the plunger at the end
    # of a stroke: over-aspirate on pump 1's chip-aspirate to cancel it
    # (firmware can't be tuned below
    # cutoff=800 inc/s; slope=1 helps but doesn't eliminate). The same volume
    # is added to the waste-dispense so the syringe still empties to home.
    # Set empirically based on visible backflow. 0.0 = compensation disabled.
    aspirate_overshoot_ul: float = 0.0

    # Chip-side ports (override if your chip is wired to different ports).
    # These stay the SINGLE ports used by dose / feed / swap_chip.
    chip_inflow_port: int = DEFAULT_CHIP_INFLOW_PORT
    chip_outflow_port: int = DEFAULT_CHIP_OUTFLOW_PORT

    # Named chip-side ports for a four-port chip: one chamber with an inlet
    # and an outlet at each of two heights.
    #     high_in, low_in    -> dispense pump (addr 0), role="chip"
    #     high_out, low_out  -> aspirate pump (addr 1), role="chip"
    # Populated from the YAML ``session.chip_ports`` block. Left empty on a
    # two-port rig, in which case chip_inflow_port / chip_outflow_port above
    # are used directly and every routine behaves exactly as it did before.
    chip_ports: dict[str, int] = field(default_factory=dict)

    # Aspirate (pump 1) port wired to the H2O2 sleep-cleaning source.
    # The sleep routine uses pump 1 (the chip-drain pump) to flush its own
    # syringe with H2O2 to waste — needs to know which valve port has the
    # H2O2 reservoir on pump 1 specifically (separate from the dispense-pump
    # H2O2 port, which lives in the reagent registry).
    aspirate_pump_h2o2_port: int | None = None

    # Auto syringe-wash configuration (NOT chip wash — this is the dispense
    # pump's syringe being flushed with PBS to waste between reagent switches)
    syringe_wash_reagent_name: str = "PBS"
    syringe_wash_cycles: int = 3

    # Chip-wash configuration (NOT syringe wash — this is replacing fluid IN
    # the chip well by running chip_swap N times with the same reagent)
    chip_wash_cycles: int = 3

    # Volume of the tubing run from the source reservoir to the pump valve
    # (one-way). Used by deprime_source / swap_reagent_tube / prime to know
    # how much air to push to clear the line, and by the tube-prime helpers
    # to aspirate exactly enough fresh reagent to fill the line. Hardware-
    # dependent — set in YAML per device.
    tube_line_volume_ul: float = 780.0

    # State carried between calls
    last_source: Reagent | None = None

    # Pre-staged dose state. Set by dose(prep_next=...) running in a background
    # thread during a recording; consumed by the NEXT dose() call to skip the
    # source-aspirate step so the new reagent hits the well within ~30s of
    # the phase boundary instead of ~90s. Keys:
    #   "reagent_name": str  — the reagent already loaded into pump 0's syringe
    #   "fill_inc":     int  — increments of reagent loaded
    #   "air_inc":      int  — increments of air backpad loaded on top
    pending_prep: dict | None = None
    _prep_thread: Any = None  # threading.Thread running the prep

    # Event log (set by begin_recording / cleared by end_recording)
    _events_file: Any = None
    _t0_monotonic: float | None = None
    _outer_phase_cm: Any = None


# =============================================================================
# HTTP / atomic primitives
# =============================================================================


# =============================================================================
# Auth
# =============================================================================

# If your rig's habitat API enforces auth, every request needs
# ``Authorization: Bearer $HABITAT_API_TOKEN``; a user-role token is enough.
# The token comes from the environment and never from a config file.
HABITAT_TOKEN_ENV = "HABITAT_API_TOKEN"

AUTH_401_HINT = (
    f"\nThis rig enforces auth. Set {HABITAT_TOKEN_ENV} in your environment "
    "to a user-role token — ask whoever administers the rig for one."
)


def _auth_token() -> str | None:
    return os.environ.get(HABITAT_TOKEN_ENV) or None


def _auth_headers() -> dict[str, str]:
    """Bearer header when a token is set, nothing when it isn't.

    Sending no header against a rig that does not enforce auth is a no-op,
    so unauthenticated rigs keep working exactly the same.
    """
    token = _auth_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def preflight_auth(url: str) -> None:
    """Fail early and legibly when the rig enforces auth and we have no token.

    Worth doing explicitly rather than letting the first 401 speak for itself:
    the first call any session makes is the syringe-volume read inside
    ``_sync_inc_per_ul``, which catches every exception and downgrades it to a
    warning. A 401 there is swallowed, INC_PER_UL silently keeps the 2500 uL
    default, and the run continues moving wrong volumes until something else
    fails with a less obvious message.
    """
    token = _auth_token()
    unset = (
        f"This rig enforces auth and {HABITAT_TOKEN_ENV} is not set.\n"
        "Set it to a user-role token — ask whoever administers the rig for one."
    )
    try:
        status = _get_json(url, "/auth/status", timeout_s=10.0)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 404:
            return  # habitat predating the auth surface — nothing to check
        if code == 401 and not token:
            raise SystemExit(unset) from e
        raise
    if not status.get("enforced"):
        return
    if not token:
        raise SystemExit(unset)
    # A token that is set but rejected has to fail here too. Otherwise the
    # first authenticated call is the syringe-volume read, whose 401 gets
    # swallowed into a warning, and the run dies later on the port-map fetch
    # with a bare httpx traceback that never mentions the token.
    try:
        _get_json(url, "/pumps/", timeout_s=10.0)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            raise SystemExit(
                f"{HABITAT_TOKEN_ENV} is set but the rig rejected it "
                f"(HTTP {e.response.status_code}).\nCheck that it's a "
                "current, valid user-role token for this rig."
            ) from e
        raise


def _rate_limit_wait_s(r: "httpx.Response", attempt: int) -> float:
    """Seconds to wait before retrying a 429.

    Prefers the server's own hint — the ``Retry-After`` header, then the
    problem body's ``retry_after_s`` — and honors it up to
    ``RATE_LIMIT_MAX_HINT_S``. Falls back to blind exponential backoff only
    when the server said nothing.
    """
    hint: Any = r.headers.get("Retry-After")
    if hint is None:
        try:
            hint = r.json().get("retry_after_s")
        except Exception:
            hint = None
    if hint is not None:
        try:
            return min(float(hint), RATE_LIMIT_MAX_HINT_S)
        except (TypeError, ValueError):
            pass
    return min(RATE_LIMIT_BASE_BACKOFF_S * (2 ** attempt), RATE_LIMIT_MAX_BACKOFF_S)


def _is_rig_busy(r: "httpx.Response") -> bool:
    """True if a 409 is Habitat's "another job is running" rejection."""
    try:
        problem = r.json()
    except Exception:
        return False
    if not isinstance(problem, dict):
        return False
    return (problem.get("title") == "RigBusy"
            or str(problem.get("type", "")).endswith("rig-busy"))


def _post_atomic(url: str, name: str, params: dict) -> dict:
    """POST /atomics/{name} with body {"params": {...}}.

    Blocks until the job reaches a terminal state and returns its result —
    motion atomics take ``wait_for_motion`` (default true), so the pump has
    finished moving by the time this returns. ``_wait_ready`` afterwards is
    a cheap post-condition check, not the completion barrier it used to be.

    Serialized process-wide by ``_ATOMIC_LOCK``: Habitat only runs one
    externally-submitted job at a time, so overlapping submissions from two
    of our threads earn a 409 instead of running concurrently.

    Retries on HTTP 429 (rate limit) and on 409 rig-busy, honoring the
    server's Retry-After header when present.
    """
    target = f"{url.rstrip('/')}/atomics/{name}"
    body = {"params": params}
    rate_limit_attempts = 0
    rig_busy_attempts = 0
    with _ATOMIC_LOCK:
        while True:
            r = httpx.post(target, json=body, timeout=300.0,
                           headers=_auth_headers())

            if r.status_code == 429 and rate_limit_attempts < RATE_LIMIT_MAX_RETRIES:
                wait_s = _rate_limit_wait_s(r, rate_limit_attempts)
                # Log the body on the first hit so we can see the policy details
                # (limit, tier, retry_after_s) — but suppress on later attempts to
                # avoid drowning the log in identical messages.
                extra = ""
                if rate_limit_attempts == 0 and r.text:
                    extra = f"  body={r.text[:200]}"
                rate_limit_attempts += 1
                pump_log(f"RATE-LIMIT 429 on {name} — sleeping {wait_s:.1f}s (attempt {rate_limit_attempts}/{RATE_LIMIT_MAX_RETRIES}){extra}")
                time.sleep(wait_s)
                continue

            if (r.status_code == 409 and _is_rig_busy(r)
                    and rig_busy_attempts < RIG_BUSY_MAX_RETRIES):
                wait_s = min(RIG_BUSY_BASE_BACKOFF_S * (2 ** rig_busy_attempts),
                             RIG_BUSY_MAX_BACKOFF_S)
                rig_busy_attempts += 1
                pump_log(
                    f"RIG-BUSY 409 on {name} — another job holds the rig; "
                    f"sleeping {wait_s:.1f}s (attempt {rig_busy_attempts}/{RIG_BUSY_MAX_RETRIES})"
                )
                time.sleep(wait_s)
                continue

            if r.status_code >= 400:
                # Print the response body so we can diagnose 400/500 etc. — Habitat
                # returns structured problem+json with useful detail in `detail`.
                body_preview = r.text[:500] if r.text else "<empty body>"
                hint = AUTH_401_HINT if r.status_code == 401 else ""
                pump_log(f"ERROR HTTP {r.status_code} on {name}: {body_preview}{hint}")
            r.raise_for_status()
            return r.json() if r.content else {}


def _get_json(url: str, path: str, timeout_s: float = 10.0) -> Any:
    """GET a Habitat endpoint, retrying on 429 the way _post_atomic does.

    Plain reads were previously unguarded, so a rate-limited session start
    raised instead of backing off.
    """
    target = f"{url.rstrip('/')}{path}"
    attempts = 0
    while True:
        r = httpx.get(target, timeout=timeout_s, headers=_auth_headers())
        if r.status_code == 429 and attempts < RATE_LIMIT_MAX_RETRIES:
            wait_s = _rate_limit_wait_s(r, attempts)
            attempts += 1
            pump_log(f"RATE-LIMIT 429 on GET {path} — sleeping {wait_s:.1f}s "
                     f"(attempt {attempts}/{RATE_LIMIT_MAX_RETRIES})")
            time.sleep(wait_s)
            continue
        if r.status_code == 401:
            pump_log(f"ERROR HTTP 401 on GET {path}{AUTH_401_HINT}")
        r.raise_for_status()
        return r.json()


def _wait_ready(url: str, addr: int, timeout_s: float = READY_TIMEOUT_S) -> None:
    """Poll centris.observation.is_ready until pump is idle. Raises on timeout.

    Motion atomics pass ``wait_for_motion=True`` and the server holds the
    request until the job reaches a terminal state, so motion helpers do NOT
    need this. It remains for control atomics that expose no motion
    barrier of their own — without it, a following command can hit
    "Command buffer overflow" / ValveStall on a still-busy driver.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = _post_atomic(url, "centris.observation.is_ready", {"addr": addr})
        if result.get("value") is True:
            return
        time.sleep(READY_POLL_INTERVAL_S)
    raise RuntimeError(f"Pump addr={addr} did not become ready within {timeout_s}s")


def _valve_to(url: str, addr: int, port: int) -> None:
    # wait_for_motion is the server-side completion barrier (default true;
    # passed explicitly so a change of default can't silently un-block us).
    # It makes a follow-up is_ready poll redundant — that poll used to be
    # half of all requests against the rate limiter.
    _post_atomic(url, "centris.motion.valve_to",
                 {"addr": addr, "port": port, "wait_for_motion": True})


def _plunger_move(url: str, addr: int, direction: str, increments: int, top_speed_ul_per_s: float) -> None:
    """direction is 'aspirate' or 'dispense'.

    A move larger than the syringe's full stroke is clamped rather than sent:
    the server's range check on ``increments`` is [0, STROKE_INCREMENTS] and
    would reject it with a 400, aborting whatever routine issued it. Callers
    that add a compensation volume on top of a full-syringe request trip this
    — e.g. drain_chip's aspirate_overshoot_ul on top of a 1000uL drain, which
    is exactly a full stroke on a 1000uL syringe.

    The clamp is loud: a silently short move is still a wrong volume, and the
    operator needs to know the compensation did not fully apply.

    Note this bounds the move against the whole stroke, not against the
    plunger's remaining travel. A relative move that is individually legal can
    still overrun from a non-zero position; the server rejects that case, and
    checking it here would cost a position read per move.
    """
    inc = int(increments)
    if inc > STROKE_INCREMENTS:
        pump_log(
            f"WARNING: {direction} of {inc} inc ({inc / INC_PER_UL:.1f}uL) exceeds the "
            f"full stroke of {STROKE_INCREMENTS} inc ({STROKE_INCREMENTS / INC_PER_UL:.1f}uL "
            f"syringe) — clamping to one full stroke; {(inc - STROKE_INCREMENTS) / INC_PER_UL:.1f}uL "
            f"of the requested volume will NOT move"
        )
        inc = STROKE_INCREMENTS
    _post_atomic(url, "centris.motion.plunger_relative", {
        "addr": addr,
        "direction": direction,
        "increments": inc,
        "top_speed_ul_per_s": float(top_speed_ul_per_s),
        "wait_for_motion": True,
    })


def _plunger_home(url: str, addr: int, top_speed_ul_per_s: float | None = None) -> None:
    """Drive the plunger to absolute position 0 (home), dispensing whatever
    is currently in the syringe through whichever valve port is selected.

    Always set the valve to a SAFE destination (e.g. waste) before calling —
    if the valve is on a source port this will push syringe contents back
    into the source bottle.
    """
    params: dict[str, Any] = {"addr": int(addr), "wait_for_motion": True}
    if top_speed_ul_per_s is not None:
        params["top_speed_ul_per_s"] = float(top_speed_ul_per_s)
    _post_atomic(url, "centris.motion.plunger_home", params)


def _ul_to_inc(volume_ul: float) -> int:
    return int(round(float(volume_ul) * INC_PER_UL))


def home_pumps(session: Session, *, pumps: list[int] | None = None,
               speed_ul_per_s: float | None = None) -> None:
    """Drive every pump's valve to waste and plunger to 0.

    Every script is a fresh process that builds a Session and assumes the
    syringes it inherits are empty. Nothing enforced that: a script that
    leaves a syringe partly full (04_sleep.py's H2O2 soak) hands that state
    to whatever runs next, and the next script's own volume request can
    then overrun the syringe — 03_experiment_control.py run right after
    04_sleep.py aspirated 760uL PBS onto a syringe already holding 500uL
    H2O2 and hit PlungerStall. make_session() calls this automatically so
    every script starts from a known-empty syringe regardless of what the
    previous process left behind. Pass ``home_pumps=False`` to make_session
    / the YAML session block to skip it (e.g. mid-experiment tooling that
    intentionally wants to inspect state before touching anything).

    Best-effort per pump per step: a stalled plunger or an unresponsive
    valve logs and continues rather than raising — this runs inside every
    session construction, so an exception here would block every script
    on a rig with one bad pump.
    """
    if pumps is None:
        pumps = [DISPENSE_PUMP_ADDR, ASPIRATE_PUMP_ADDR]
    speed = speed_ul_per_s if speed_ul_per_s is not None else session.offchip_speed_code
    for addr in pumps:
        try:
            waste_port = _find_port(session.pump_map[addr], role="waste")
        except Exception as e:
            pump_log(f"HOME pump {addr}: no waste port in the live map ({e!r}) — skipping")
            continue
        try:
            _valve_to(session.url, addr, waste_port)
        except Exception as e:
            pump_log(f"HOME pump {addr}: valve->waste failed ({e!r}) — continuing")
        try:
            _plunger_home(session.url, addr, top_speed_ul_per_s=speed)
        except Exception as e:
            pump_log(f"HOME pump {addr}: plunger_home failed ({e!r}) — continuing")
        pump_log(f"HOME pump {addr}: valve -> waste port {waste_port}, plunger -> 0")


def _run_serial(steps: list) -> None:
    """Run callables one after another, in the order given.

    These used to run as parallel daemon threads (two pumps moving at once).
    Habitat serializes hardware access — only one externally-submitted job
    runs at a time — so the second pump's atomic came back 409 rig-busy, and
    because a dead daemon thread only prints a traceback, the caller logged
    "done" and carried on with the motion never having happened. Running the
    steps in order is what the hardware does anyway, and an exception now
    propagates to the caller instead of being swallowed.
    """
    for step in steps:
        step()


def _split_increments(total_inc: int, chunk_ul: float) -> list[int]:
    """Split a plunger delta into chunks of at most ``chunk_ul``. Sums exact."""
    chunk_inc = max(1, _ul_to_inc(chunk_ul))
    chunks = []
    remaining = int(total_inc)
    while remaining > 0:
        step = min(chunk_inc, remaining)
        chunks.append(step)
        remaining -= step
    return chunks


# =============================================================================
# Port-map / reagent registry
# =============================================================================


def _fetch_port_map(url: str) -> dict[int, dict[int, dict]]:
    """GET /config/port-map and reshape into {addr: {port: info}}."""
    data = _get_json(url, "/config/port-map", timeout_s=10.0)
    out: dict[int, dict[int, dict]] = {}
    for pump in data.get("pumps", []):
        out[pump["addr"]] = {p["port"]: p for p in pump["port_map"]}
    return out


def _find_port(pump_map: dict[int, dict], role: str, chip_id: str | None = None) -> int:
    """First port on a pump with matching role (and optionally chip_id)."""
    for port, info in pump_map.items():
        if info["role"] != role:
            continue
        if chip_id is not None and info.get("chip_id") != chip_id:
            continue
        return port
    raise RuntimeError(f"No port with role={role!r} chip_id={chip_id!r} in pump map")


# Named chip ports for a four-port chip. Inflows live on the dispense pump,
# outflows on the aspirate pump. Order matters for sweeps: outflows are listed
# HIGH FIRST so a multi-port drain finishes on the low port and actually
# empties the chamber instead of stopping at the high port's height.
CHIP_INFLOW_PORT_NAMES = ("low_in", "high_in")
CHIP_OUTFLOW_PORT_NAMES = ("high_out", "low_out")
CHIP_PORT_NAMES = CHIP_INFLOW_PORT_NAMES + CHIP_OUTFLOW_PORT_NAMES


def chip_port(session: "Session", name: str) -> int:
    """Resolve a named chip port ("high_in", "low_out", ...) to a port number.

    Raises rather than guessing — a typo or a missing YAML entry should fail
    before any motion, not silently address the wrong port on the valve.
    """
    if name not in CHIP_PORT_NAMES:
        raise KeyError(f"Unknown chip port name {name!r}. Known: {list(CHIP_PORT_NAMES)}")
    if name not in session.chip_ports:
        raise KeyError(
            f"Chip port {name!r} is not configured. Set session.chip_ports in the "
            f"YAML. Configured: {sorted(session.chip_ports)}"
        )
    return session.chip_ports[name]


def chip_inflow_ports(session: "Session") -> list[int]:
    """Every chip-side inflow port on the dispense pump, low port first.

    Prefers the explicit ``session.chip_ports`` mapping. Falls back to
    autodiscovery (every ``role="chip"`` entry on pump 0) so two-port rigs
    that never set chip_ports keep working, and finally to the single
    ``chip_inflow_port``.
    """
    named = [session.chip_ports[n] for n in CHIP_INFLOW_PORT_NAMES if n in session.chip_ports]
    if named:
        return named
    disp = session.pump_map.get(DISPENSE_PUMP_ADDR, {})
    found = sorted(p for p, info in disp.items() if info.get("role") == "chip")
    return found or [session.chip_inflow_port]


def chip_outflow_ports(session: "Session") -> list[int]:
    """Every chip-side outflow port on the aspirate pump, HIGH PORT FIRST.

    The order is the point: callers that sweep this list (drain_chip,
    purge_chip_ports) finish on the low port, which is the one that can pull
    the chamber down to empty.
    """
    named = [session.chip_ports[n] for n in CHIP_OUTFLOW_PORT_NAMES if n in session.chip_ports]
    if named:
        return named
    asp = session.pump_map.get(ASPIRATE_PUMP_ADDR, {})
    found = sorted(p for p, info in asp.items() if info.get("role") == "chip")
    return found or [session.chip_outflow_port]


def _reagents_from_port_map(pump_map: dict[int, dict[int, dict]]) -> dict[str, Reagent]:
    """Default reagent registry: derive from dispense pump's port_map labels."""
    registry: dict[str, Reagent] = {}
    disp = pump_map.get(DISPENSE_PUMP_ADDR, {})
    for port, info in disp.items():
        role = info.get("role", "")
        # Skip non-source ports (chip / waste / air are not user-selectable reagents)
        if role in ("chip", "waste", "air"):
            continue
        label = info.get("label", "") or f"port_{port}"
        # Map Habitat's port_map roles to ours
        # "media" -> "media", "reagent" -> "drug" (default; user can override),
        # "buffer" -> "buffer", anything else passes through.
        our_role = {"reagent": "drug"}.get(role, role)
        registry[label] = Reagent(name=label, port=port, role=our_role, label=label)
    return registry


# =============================================================================
# Session construction
# =============================================================================


def make_session(
    url: str,
    chip_id: str,
    *,
    reagents: dict[str, Reagent] | None = None,
    working_volume_ul: float = 100.0,
    air_backpad_ul: float = 200.0,
    slow_speed_code: int = 26,
    offchip_speed_code: int = 17,
    dry_pause_s: float = 0.0,
    chip_inflow_port: int | None = None,
    chip_outflow_port: int | None = None,
    chip_ports: dict[str, int] | None = None,
    syringe_wash_reagent_name: str = "PBS",
    syringe_wash_cycles: int = 3,
    chip_wash_cycles: int = 3,
    tube_line_volume_ul: float = 1000.0,
    aspirate_overshoot_ul: float = 0.0,
    ramp_slope_code: int | None = 1,
    aspirate_pump_h2o2_port: int | None = None,
    home_pumps_on_start: bool = True,
) -> Session:
    """Build a Session, pulling port_map from the live Habitat instance.

    If ``reagents`` is None, the registry is derived from the dispense pump's
    port_map labels — useful when your reagents are already configured in
    Habitat. Pass an explicit dict to register custom names (e.g.,
    ``olanzapine_low/mid/high``) or to override the role-mapping.

    ``ramp_slope_code`` (default 1) is set on both pumps at session start —
    the gentlest ramp reduces (but doesn't eliminate) the mechanical
    reverse-creep at end of stroke described above. Pass ``None`` to skip.

    ``aspirate_overshoot_ul`` (default 0.0) over-aspirates pump 1's chip-drain
    by this many uL to fully compensate for the remaining reverse-creep.
    Tune empirically: increase until the well looks fully drained.

    ``home_pumps_on_start`` (default True) drives every pump's valve to
    waste and plunger to 0 before the session is handed back — see
    ``home_pumps()``. Pass False to skip (e.g. inspecting state before
    touching anything).

    ``chip_ports`` names the ports of a four-port chip — one chamber with an
    inlet and an outlet at each of two heights:
    ``{"high_in": 4, "low_in": 5, "high_out": 6, "low_out": 7}``. Inflows
    must be on pump 0, outflows on pump 1. When given and the singular
    ``chip_inflow_port`` / ``chip_outflow_port`` are left as None, the
    defaults every existing routine uses become ``high_in`` (fill from above)
    and ``low_out`` (drain from the bottom, so the chamber can reach empty).
    """
    preflight_auth(url)
    _sync_inc_per_ul(url)
    pump_map = _fetch_port_map(url)
    if reagents is None:
        reagents = _reagents_from_port_map(pump_map)

    # Validate chip_ports up front: an unrecognised key here is a YAML typo,
    # and the cost of finding out later is a valve addressing the wrong port
    # on a chip with a sample in it.
    chip_ports = dict(chip_ports or {})
    unknown_chip_ports = sorted(set(chip_ports) - set(CHIP_PORT_NAMES))
    if unknown_chip_ports:
        raise ValueError(
            f"Unknown chip_ports key(s): {unknown_chip_ports}. "
            f"Valid names: {list(CHIP_PORT_NAMES)}"
        )
    # A null in the YAML means the operator hasn't filled the port in yet.
    # Say so here rather than letting None reach _valve_to() as a port number.
    unset_chip_ports = sorted(k for k, v in chip_ports.items() if v is None)
    if unset_chip_ports:
        raise ValueError(
            f"chip_ports {unset_chip_ports} have no port number. Set them in "
            f"the YAML session.chip_ports block."
        )
    chip_ports = {k: int(v) for k, v in chip_ports.items()}
    # The singular ports stay what dose / feed / swap_chip reach for. Fill
    # from the top, drain from the bottom.
    if chip_inflow_port is None:
        chip_inflow_port = chip_ports.get(
            "high_in", chip_ports.get("low_in", DEFAULT_CHIP_INFLOW_PORT))
    if chip_outflow_port is None:
        chip_outflow_port = chip_ports.get(
            "low_out", chip_ports.get("high_out", DEFAULT_CHIP_OUTFLOW_PORT))

    # Lock in the gentlest ramp slope on both pumps at session start. Must
    # _wait_ready after this control atomic too (it has no motion barrier
    # of its own), otherwise the next command can hit "Command buffer
    # overflow" / ValveStall.
    if ramp_slope_code is not None:
        for addr in (DISPENSE_PUMP_ADDR, ASPIRATE_PUMP_ADDR):
            try:
                _post_atomic(url, "centris.control.set_ramp_slope",
                             {"addr": addr, "slope_code": int(ramp_slope_code)})
                _wait_ready(url, addr)
            except httpx.HTTPError as e:
                pump_log(f"warning: could not set ramp_slope={ramp_slope_code} on addr={addr}: {e}")

    session = Session(
        url=url,
        chip_id=chip_id,
        reagents=reagents,
        pump_map=pump_map,
        working_volume_ul=working_volume_ul,
        air_backpad_ul=air_backpad_ul,
        slow_speed_code=slow_speed_code,
        offchip_speed_code=offchip_speed_code,
        dry_pause_s=dry_pause_s,
        chip_inflow_port=chip_inflow_port,
        chip_outflow_port=chip_outflow_port,
        chip_ports=chip_ports,
        syringe_wash_reagent_name=syringe_wash_reagent_name,
        syringe_wash_cycles=syringe_wash_cycles,
        chip_wash_cycles=chip_wash_cycles,
        tube_line_volume_ul=tube_line_volume_ul,
        aspirate_overshoot_ul=aspirate_overshoot_ul,
        aspirate_pump_h2o2_port=aspirate_pump_h2o2_port,
    )
    if home_pumps_on_start:
        home_pumps(session, speed_ul_per_s=offchip_speed_code)
    return session


def load_session_from_yaml(config_path: str) -> tuple[Session, dict]:
    """Load a Session + protocol-shape params from a YAML config.

    Expected schema:
        habitat:
          url: <str>                       # required
          chip_id: <str>                   # required
        session:                           # all optional; Session defaults used
          working_volume_ul: <float>
          air_backpad_ul: <float>
          slow_speed_code: <int>
          offchip_speed_code: <int>
          dry_pause_s: <float>
          chip_inflow_port: <int>          # default: chip_ports.high_in
          chip_outflow_port: <int>         # default: chip_ports.low_out
          chip_ports:                      # optional; four-port chips
            high_in: <int>                 #   inflows  -> pump 0
            low_in: <int>
            high_out: <int>                #   outflows -> pump 1
            low_out: <int>
          syringe_wash_reagent_name: <str>
          syringe_wash_cycles: <int>
          chip_wash_cycles: <int>
          tube_line_volume_ul: <float>
          aspirate_overshoot_ul: <float>
          ramp_slope_code: <int|null>
          home_pumps_on_start: <bool>       # default true; see home_pumps()
        reagents:                          # optional; falls back to port_map labels
          <name>:
            port: <int>                    # required (within each reagent)
            role: <"media"|"drug"|"wash"|"buffer"|"reagent">   # default "reagent"
            label: <str>                   # optional
          ...
        protocol:                          # opaque pass-through (returned as-is)
          ...

    Returns (session, protocol_dict). The caller consumes ``protocol_dict``
    for its own protocol-shape parameters (durations, dose order, etc.).
    """
    import yaml

    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    habitat = cfg.get("habitat", {})
    session_cfg = cfg.get("session", {}) or {}
    reagents_cfg = cfg.get("reagents", {}) or {}
    protocol_cfg = cfg.get("protocol", {}) or {}

    if "url" not in habitat:
        raise ValueError(f"{config_path}: missing required habitat.url")
    if "chip_id" not in habitat:
        raise ValueError(f"{config_path}: missing required habitat.chip_id")

    reagents: dict[str, Reagent] | None = None
    if reagents_cfg:
        reagents = {}
        for name, kw in reagents_cfg.items():
            if "port" not in kw:
                raise ValueError(f"{config_path}: reagent {name!r} missing required 'port'")
            reagents[name] = Reagent(
                name=name,
                port=int(kw["port"]),
                role=kw.get("role", "reagent"),
                label=kw.get("label", name),
            )

    session = make_session(
        url=habitat["url"],
        chip_id=habitat["chip_id"],
        reagents=reagents,
        **session_cfg,
    )
    return session, protocol_cfg


def _get_reagent(session: Session, name: str) -> Reagent:
    if name not in session.reagents:
        raise KeyError(f"Reagent {name!r} not in registry. Known: {sorted(session.reagents)}")
    return session.reagents[name]


# =============================================================================
# Logging
# =============================================================================


def pump_log(msg: str) -> None:
    """Print a pump-action message with millisecond timestamp."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


@contextmanager
def _phase(session: Session, name: str):
    """Bracket a phase and write one JSONL record on exit (if recording).

    Each record carries start/end relative seconds, duration, and absolute
    unix start/end timestamps. Nested phases nest naturally; inner records
    are written first (on inner block exit), outer last.
    """
    if session._events_file is None or session._t0_monotonic is None:
        # Not recording — pass through.
        yield
        return

    t_start_mono = time.monotonic()
    t_start_dt = datetime.now().astimezone()
    try:
        yield
    finally:
        t_end_mono = time.monotonic()
        t_end_dt = datetime.now().astimezone()
        session._events_file.write(json.dumps({
            "event": name,
            "t_start_rel_s": round(t_start_mono - session._t0_monotonic, 3),
            "t_end_rel_s": round(t_end_mono - session._t0_monotonic, 3),
            "duration_s": round(t_end_mono - t_start_mono, 3),
            "t_start": t_start_dt.isoformat(timespec="milliseconds"),
            "t_end":   t_end_dt.isoformat(timespec="milliseconds"),
        }) + "\n")
        session._events_file.flush()


# =============================================================================
# Routines
# =============================================================================


def _pump_through_to_waste(session: Session, reagent: Reagent, *,
                           cycles: int, volume_ul: float,
                           log_tag: str) -> None:
    """Aspirate ``volume_ul`` from ``reagent.port``, dispense to the
    dispense-pump's waste port, repeat ``cycles`` times. Pure mechanism;
    the caller picks ``log_tag`` so the message reflects the SEMANTIC
    purpose (PRIME = filling a source line for use; WASH = cleaning the
    syringe barrel; DEPRIME / etc).
    """
    inc = _ul_to_inc(volume_ul)
    waste_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="waste")
    speed = session.offchip_speed_code

    pump_log(
        f"{log_tag:<6} start  reagent={reagent.name} src_port={reagent.port} "
        f"vol={volume_ul:.1f}uL ({inc} inc) cycles={cycles} "
        f"speed_code={speed} -> waste_port={waste_port}"
    )
    t_start = time.monotonic()
    for _ in range(int(cycles)):
        _valve_to(session.url, DISPENSE_PUMP_ADDR, reagent.port)
        _plunger_move(session.url, DISPENSE_PUMP_ADDR, "aspirate", inc, speed)
        _valve_to(session.url, DISPENSE_PUMP_ADDR, waste_port)
        _plunger_move(session.url, DISPENSE_PUMP_ADDR, "dispense", inc, speed)
    pump_log(f"{log_tag:<6} done   in {time.monotonic() - t_start:.1f}s")


def prime(session: Session, reagent_name: str, *, cycles: int = 1,
          volume_ul: float | None = None) -> None:
    """Prime one source line (source -> syringe -> waste). Chip uninvolved.

    Default volume is ``session.tube_line_volume_ul`` so the line gets a
    full tube-length of fresh reagent. Pass an explicit ``volume_ul`` for
    larger / smaller prime cycles.
    """
    r = _get_reagent(session, reagent_name)
    vol = float(volume_ul) if volume_ul is not None else session.tube_line_volume_ul
    _pump_through_to_waste(session, r, cycles=cycles, volume_ul=vol, log_tag="PRIME")


def deprime_source(session: Session, reagent_name: str, *,
                    air_volume_ul: float | None = None) -> None:
    """Push air through the source line to drive residual reagent back into
    its container, then leave the line dry/air-filled.

    Used before manually disconnecting a tube during a reagent swap: the
    aspirate-side of pump 0 (source -> valve -> syringe path) is cleared
    so the user can pull the tube off without dripping, and any reagent
    sitting in the tubing is recovered into the source container.

    Sequence on pump 0:
        valve -> air, aspirate air_volume_ul   (load air slug)
        valve -> source, dispense air_volume_ul (push air through source line)

    ``air_volume_ul`` defaults to ``session.tube_line_volume_ul * 1.2`` —
    20%% more than the tube volume so we know the line is fully clear.
    """
    r = _get_reagent(session, reagent_name)
    vol = float(air_volume_ul) if air_volume_ul is not None else session.tube_line_volume_ul * 1.2
    inc = _ul_to_inc(vol)
    air_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")
    speed = session.offchip_speed_code

    pump_log(
        f"DEPRIME start reagent={r.name} src_port={r.port} air_port={air_port} "
        f"air_volume={vol:.1f}uL ({inc} inc) — pushing residual reagent back to source"
    )
    t_start = time.monotonic()
    _valve_to(session.url, DISPENSE_PUMP_ADDR, air_port)
    _plunger_move(session.url, DISPENSE_PUMP_ADDR, "aspirate", inc, speed)
    _valve_to(session.url, DISPENSE_PUMP_ADDR, r.port)
    _plunger_move(session.url, DISPENSE_PUMP_ADDR, "dispense", inc, speed)
    pump_log(f"DEPRIME done  in {time.monotonic() - t_start:.1f}s")


def swap_reagent_tube(session: Session, *,
                      current_reagent: str | None,
                      next_reagent: str | None,
                      prime_cycles: int = 1,
                      prompt: bool = True,
                      wash_after: bool = False) -> None:
    """Coordinate a manual tube swap on a shared source port.

    Workflow (one or both sides can be None for first-prime / final-purge):

        1. If ``current_reagent``: deprime_source(current) — recover line
           residue back into its container so the user can disconnect cleanly.
        2. If ``prompt``: input("...press Enter") — wait for the user to
           physically swap the tubing.
        3. If ``next_reagent``: prime(next, cycles=prime_cycles, volume_ul=
           tube_line_volume_ul) — fill the line with fresh reagent and
           discard the priming volume to waste.
        4. If ``wash_after``: wash_syringe(session) — flush the dispense
           pump's syringe with PBS to waste so residual potent drug doesn't
           contaminate the next chip operation. Use when the new tube is a
           potent drug that hasn't been applied to the tissue yet.

    Common use-case: an experiment runs three concentrations of the same
    drug (e.g., olanzapine low/mid/high) through a single valve port, with
    the user manually swapping the source tube between concentrations.
    """
    if current_reagent is not None:
        deprime_source(session, current_reagent)

    if prompt and current_reagent is not None and next_reagent is not None:
        msg = (
            f"\n>>> Swap source tube from {current_reagent!r} -> {next_reagent!r}. "
            f"Press Enter when done..."
        )
        _wait_for_enter(msg)
    elif prompt and current_reagent is None and next_reagent is not None:
        msg = f"\n>>> Connect source tube for {next_reagent!r}. Press Enter when done..."
        _wait_for_enter(msg)
    elif prompt and current_reagent is not None and next_reagent is None:
        pump_log(f"Tube for {current_reagent!r} is purged. You can disconnect it.")

    if next_reagent is not None:
        # Prime exactly the tube-line volume so the line is filled with fresh
        # reagent and excess (the priming aspirate) goes to waste.
        prime(session, next_reagent, cycles=prime_cycles,
              volume_ul=session.tube_line_volume_ul)
        # Reflect the new last-source so a following dose() of the same
        # reagent doesn't trigger a redundant syringe wash.
        session.last_source = _get_reagent(session, next_reagent)

        if wash_after:
            # Defensive PBS wash — the syringe just drew a potent drug;
            # flush before the next chip operation so residual doesn't
            # contaminate downstream calls. Note: this leaves last_source
            # set to PBS so the next dose() of the same reagent triggers
            # an auto-wash anyway — set last_source back to next_reagent
            # after, since we did already prime the line cleanly.
            pump_log(
                f"SWAP   wash_after=True — flushing syringe with "
                f"{session.syringe_wash_reagent_name}"
            )
            wash_syringe(session)
            session.last_source = _get_reagent(session, next_reagent)


def prime_reagents(session: Session, reagents: list[str] | None = None, *,
                   volume_ul: float | None = None,
                   cycles: int = 1,
                   wash_between: bool = False) -> None:
    """Prime one or more reagent source lines (source -> waste).

    ``reagents`` is a list of reagent names. If ``None``, primes every
    reagent in the session registry except the wash reagent (typically PBS)
    — since PBS is used by wash_syringe and doesn't need a separate prime.

    Default per-cycle volume is ``session.tube_line_volume_ul`` — enough
    to fill the entire tubing run from source to pump with fresh reagent.

    ``wash_between=True`` inserts a syringe-wash between any two reagents
    whose roles differ (per ``_should_auto_wash``). Use this for the
    "drugs first, PBS, media last" pattern when priming the whole rack.
    """
    if reagents is None:
        reagents = [name for name in session.reagents
                    if name != session.syringe_wash_reagent_name]
    vol = float(volume_ul) if volume_ul is not None else session.tube_line_volume_ul

    # Deduplicate by source port — if multiple reagent NAMES share the same
    # physical valve port (e.g. olanzapine_low/mid/high all on port 7,
    # swapped by hand mid-experiment), priming the line once is enough.
    # Only ONE concentration is physically connected at a time, so the
    # name we use to log it is the FIRST one given in ``reagents`` for
    # that port — that's what the operator has currently hooked up.
    seen_ports: dict[int, str] = {}
    deduped: list[str] = []
    skipped: list[tuple[str, str]] = []  # (skipped_name, first_name_on_that_port)
    for name in reagents:
        r = _get_reagent(session, name)
        if r.port in seen_ports:
            skipped.append((name, seen_ports[r.port]))
            continue
        seen_ports[r.port] = name
        deduped.append(name)
    for skipped_name, first_name in skipped:
        pump_log(
            f"PRIME-MULTI skip {skipped_name!r}: shares port with "
            f"{first_name!r} (one physical tube, already primed by {first_name!r})"
        )

    pump_log(
        f"PRIME-MULTI start reagents={deduped} volume={vol:.1f}uL "
        f"cycles={cycles} wash_between={wash_between}"
    )
    t_start = time.monotonic()
    last: Reagent | None = None
    for name in deduped:
        r = _get_reagent(session, name)
        if wash_between and last is not None and _should_auto_wash(last, r):
            wash_syringe(session)
        prime(session, name, cycles=cycles, volume_ul=vol)
        last = r
    if last is not None:
        session.last_source = last
    pump_log(f"PRIME-MULTI done in {time.monotonic() - t_start:.1f}s")


#: Port roles that ``deprime_all_lines`` never touches (chip-side ports are
#: handled by chip-purge routines; air ports are already air).
DEPRIME_SKIP_ROLES: set[str] = {"air", "chip"}


def deprime_all_lines(session: Session, pump_addr: int, *,
                      include_roles: set[str] | None = None,
                      exclude_roles: set[str] | None = None,
                      air_volume_ul: float | None = None) -> None:
    """Push air through every matching non-air, non-chip port on the given
    pump's valve so the selected source / waste / cleanup lines end air-
    filled.

    For each target port: valve -> air, aspirate air; valve -> target,
    dispense — pushes any residual through the tube to its destination
    (reagent bottle / waste container). Chip-role and air-role ports are
    ALWAYS skipped (chip is handled by chip_swap/flush_chip flows; air
    doesn't need depriming).

    ``include_roles`` (set of role names) restricts targets to just those
    roles. ``exclude_roles`` removes specific roles from the default
    target set. Pass ``include_roles={"waste"}`` to deprime only waste
    lines; pass ``exclude_roles={"waste"}`` to deprime sources only.

    Quietly skips this pump if it has no air-role port (we'd have no way
    to load an air slug). Used to leave the system in a fully-dry state
    between reagents or at the end of a session.
    """
    url = session.url
    speed = session.offchip_speed_code
    air_vol = float(air_volume_ul) if air_volume_ul is not None else session.tube_line_volume_ul * 1.2
    air_inc = _ul_to_inc(air_vol)

    pump_map = session.pump_map[pump_addr]

    try:
        air_port = _find_port(pump_map, role="air")
    except Exception as e:
        pump_log(f"DEPRIME pump {pump_addr} SKIPPED: no air-role port ({e!r})")
        return

    excl = set(exclude_roles or set()) | DEPRIME_SKIP_ROLES

    def _port_matches(info: dict) -> bool:
        role = info.get("role")
        if role in excl:
            return False
        if include_roles is not None and role not in include_roles:
            return False
        return True

    targets = sorted(port for port, info in pump_map.items() if _port_matches(info))
    if not targets:
        pump_log(f"DEPRIME pump {pump_addr}: nothing matches this filter")
        return

    pump_log(
        f"DEPRIME pump {pump_addr} {len(targets)} line(s): "
        f"{[pump_map[p].get('label', f'port {p}') for p in targets]}"
    )
    for port in targets:
        info = pump_map[port]
        label = info.get("label", f"port {port}")
        role = info.get("role", "?")
        pump_log(f"DEPRIME pump {pump_addr} {label} (port {port}, role={role})")
        _valve_to(url, pump_addr, air_port)
        _plunger_move(url, pump_addr, "aspirate", air_inc, speed)
        _valve_to(url, pump_addr, port)
        _plunger_move(url, pump_addr, "dispense", air_inc, speed)


def deprime_reagents(session: Session, reagents: list[str] | None = None, *,
                     air_volume_ul: float | None = None) -> None:
    """Push air through one or more reagent source lines, returning residue
    to their source containers (calls ``deprime_source`` per reagent).

    ``reagents`` is a list of reagent names. If ``None``, depumps every
    reagent in the session registry except the wash reagent.

    Default air volume per call is ``session.tube_line_volume_ul * 1.2``
    (the same default used by ``deprime_source``).
    """
    if reagents is None:
        reagents = [name for name in session.reagents
                    if name != session.syringe_wash_reagent_name]

    # Deduplicate by source port — multiple reagent NAMES on the same
    # physical port (olanzapine_low/mid/high all on port 7) share a single
    # tube. One deprime clears that tube; doing it three times wastes air
    # and pump motion.
    seen_ports: dict[int, str] = {}
    deduped: list[str] = []
    for name in reagents:
        r = _get_reagent(session, name)
        if r.port in seen_ports:
            pump_log(
                f"DEPRIME-MULTI skip {name!r}: shares port with "
                f"{seen_ports[r.port]!r} (one physical tube)"
            )
            continue
        seen_ports[r.port] = name
        deduped.append(name)

    pump_log(f"DEPRIME-MULTI start reagents={deduped}")
    t_start = time.monotonic()
    for name in deduped:
        deprime_source(session, name, air_volume_ul=air_volume_ul)
    pump_log(f"DEPRIME-MULTI done in {time.monotonic() - t_start:.1f}s")


def wash_syringe(session: Session, *, cycles: int | None = None,
                 volume_ul: float | None = None) -> None:
    """Flush the dispense-pump syringe via the wash reagent (PBS) to waste.

    Mechanically identical to ``prime(PBS)`` — aspirate from the wash
    reagent's source port and dispense to waste, repeated — but the INTENT
    is to clean the syringe barrel between reagent classes, not to prepare
    a source line for use. Logs as ``WASH`` (not ``PRIME``) so the
    operator sees the difference at a glance.

    Defaults to ``session.syringe_wash_cycles`` cycles using
    ``session.syringe_wash_reagent_name``.
    """
    n = cycles if cycles is not None else session.syringe_wash_cycles
    if session.syringe_wash_reagent_name not in session.reagents:
        pump_log(
            f"WASH   skipped (syringe_wash_reagent_name={session.syringe_wash_reagent_name!r} "
            f"not in registry; declare it to enable syringe washes)"
        )
        return
    r = session.reagents[session.syringe_wash_reagent_name]
    vol = float(volume_ul) if volume_ul is not None else session.tube_line_volume_ul
    _pump_through_to_waste(session, r, cycles=n, volume_ul=vol, log_tag="WASH")


def _should_auto_wash(prev: Reagent, new: Reagent) -> bool:
    """Default policy: wash when switching between different reagents,
    UNLESS both are the same media-class (e.g., aCSF -> aCSF or media -> media
    of the same name).

    Wash when:
    - The reagent name changes AND
    - At least one of (prev, new) is a "drug" or "reagent" class.

    Override by passing auto_wash=False to dose().
    """
    if prev.name == new.name:
        return False
    risky = {"drug", "reagent"}
    return prev.role in risky or new.role in risky


def chip_swap(session: Session, *, source_port: int, drain_volume_ul: float,
              fill_volume_ul: float, air_backpad: bool = True,
              air_volume_ul: float | None = None,
              chip_inflow: int | None = None,
              chip_outflow: int | None = None) -> None:
    """Replace chip contents using the two-pump parallel pattern.

    Stage 1 (pump 0 sequential): load media into chip-side line + take an
    air breath. Pump 1 idle, chip still holds old fluid.
    Stage 2 (pump 1 sequential): drain the well. Pump 0 idle and ready.
    Stage 3 (parallel): pump 0 air-push delivers slug into well || pump 1
    disposes waste.

    ``chip_inflow`` / ``chip_outflow`` override the session's defaults when
    a particular swap should use different chip ports (e.g., flush_chip
    cycles through the two inflow ports on pump 0).

    This orchestrates both pumps together, rather than moving one at a time.
    """
    inflow = chip_inflow if chip_inflow is not None else session.chip_inflow_port
    outflow = chip_outflow if chip_outflow is not None else session.chip_outflow_port

    drain_inc = _ul_to_inc(drain_volume_ul)
    fill_inc = _ul_to_inc(fill_volume_ul)
    air_vol = float(air_volume_ul) if air_volume_ul is not None else session.air_backpad_ul
    air_inc = _ul_to_inc(air_vol) if air_backpad else 0

    # Over-aspirate pump 1's chip-drain (and matching waste-dispense) by
    # this many increments to cancel the reverse-creep described above.
    overshoot_inc = _ul_to_inc(session.aspirate_overshoot_ul) if session.aspirate_overshoot_ul > 0 else 0
    drain_inc_total = drain_inc + overshoot_inc

    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code

    waste_p1 = _find_port(session.pump_map[ASPIRATE_PUMP_ADDR], role="waste")
    air_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air") if air_backpad else None

    desc = (
        f"SWAP   start  chip={session.chip_id} drain={drain_volume_ul:.1f}uL "
        f"({drain_inc} inc) fill={fill_volume_ul:.1f}uL ({fill_inc} inc) "
        f"src_port={source_port} chip_in={inflow} "
        f"chip_out={outflow}"
    )
    if overshoot_inc > 0:
        desc += f" +overshoot={session.aspirate_overshoot_ul:.1f}uL ({overshoot_inc} inc)"
    if air_backpad:
        desc += f" + air_backpad={air_vol:.1f}uL ({air_inc} inc)"
    pump_log(desc)
    t_start = time.monotonic()

    # Stage 1: pump 0 fully preps in sequence.
    _valve_to(url, DISPENSE_PUMP_ADDR, int(source_port))
    _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", fill_inc, offchip)
    _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", fill_inc, slow)
    if air_backpad:
        _valve_to(url, DISPENSE_PUMP_ADDR, air_port)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)
    pump_log(f"SWAP   prep done in {time.monotonic() - t_start:.1f}s")

    # Stage 2: pump 1 drains the well (over-aspirates by overshoot_inc).
    t_drain = time.monotonic()
    _valve_to(url, ASPIRATE_PUMP_ADDR, outflow)
    _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", drain_inc_total, slow)
    pump_log(f"SWAP   drain done in {time.monotonic() - t_drain:.1f}s")

    if session.dry_pause_s > 0:
        time.sleep(session.dry_pause_s)

    # Stage 3 (parallel): push media into well || dispose waste.
    t_b = time.monotonic()

    def _push_air():
        if air_backpad:
            _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
            _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", air_inc, slow)

    def _dispose_waste():
        _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", drain_inc_total, offchip)

    # Order matters: deliver the new media into the drained well FIRST, then
    # dump pump 1's waste. The well is never left dry any longer than it was.
    _run_serial([_push_air, _dispose_waste])
    pump_log(f"SWAP   deliver+dispose done in {time.monotonic() - t_b:.1f}s")
    pump_log(f"SWAP   total in {time.monotonic() - t_start:.1f}s")


def _prep_for_next(session: Session, current_reagent: Reagent, next_reagent: Reagent) -> None:
    """Pre-stage the next dose during the current recording.

    Pure pump-0 motions (no chip contact): optional syringe wash + aspirate
    the next reagent into the syringe. Sets ``session.pending_prep`` on
    success so the next ``dose()`` call skips its own source-aspirate step.

    Run in a background thread by ``dose(prep_next=...)``.
    """
    pump_log(f"PREP-NEXT start {next_reagent.name} (during {current_reagent.name} record)")
    t_prep = time.monotonic()
    try:
        if _should_auto_wash(current_reagent, next_reagent):
            pump_log(
                f"PREP-NEXT auto-wash: {current_reagent.name} ({current_reagent.role}) "
                f"-> {next_reagent.name} ({next_reagent.role})"
            )
            wash_syringe(session)

        fill_inc = _ul_to_inc(session.working_volume_ul)
        air_inc = _ul_to_inc(session.air_backpad_ul)
        _valve_to(session.url, DISPENSE_PUMP_ADDR, next_reagent.port)
        _plunger_move(
            session.url, DISPENSE_PUMP_ADDR, "aspirate", fill_inc,
            session.offchip_speed_code,
        )
        session.pending_prep = {
            "reagent_name": next_reagent.name,
            "fill_inc": fill_inc,
            "air_inc": air_inc,
        }
        pump_log(
            f"PREP-NEXT done in {time.monotonic() - t_prep:.1f}s — "
            f"{next_reagent.name} loaded in syringe"
        )
    except Exception as e:
        pump_log(f"PREP-NEXT failed for {next_reagent.name}: {e!r} — next dose will fall back to full chip_swap")
        session.pending_prep = None


def _commit_prestaged_dose(session: Session, reagent: Reagent, *,
                           fill_inc: int, air_inc: int,
                           air_backpad: bool = True) -> None:
    """Finish a pre-staged dose: dispense reagent into the chip line, top up
    with air backpad, drain old well, parallel air-push + waste-dispose.

    Assumes pump 0's syringe already holds ``fill_inc`` of the reagent
    (loaded by ``_prep_for_next``). Skips the source-aspirate of ``chip_swap``
    Stage 1, so commit takes ~30-40 s instead of ~60-90 s.
    """
    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code
    inflow = session.chip_inflow_port
    outflow = session.chip_outflow_port
    waste_p1 = _find_port(session.pump_map[ASPIRATE_PUMP_ADDR], role="waste")
    air_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air") if air_backpad else None
    overshoot_inc = _ul_to_inc(session.aspirate_overshoot_ul) if session.aspirate_overshoot_ul > 0 else 0
    drain_inc_total = fill_inc + overshoot_inc

    pump_log(
        f"COMMIT start reagent={reagent.name} (pre-staged) "
        f"chip={session.chip_id} chip_in={inflow} chip_out={outflow}"
    )
    t_start = time.monotonic()

    # Finish Stage 1: dispense the pre-aspirated reagent into the chip-side
    # line, then aspirate the air backpad on top.
    _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", fill_inc, slow)
    if air_backpad:
        _valve_to(url, DISPENSE_PUMP_ADDR, air_port)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)
    pump_log(f"COMMIT stage1-finish done in {time.monotonic() - t_start:.1f}s")

    # Stage 2: drain old well.
    t_drain = time.monotonic()
    _valve_to(url, ASPIRATE_PUMP_ADDR, outflow)
    _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", drain_inc_total, slow)
    pump_log(f"COMMIT drain done in {time.monotonic() - t_drain:.1f}s")

    if session.dry_pause_s > 0:
        time.sleep(session.dry_pause_s)

    # Stage 3 (parallel): pump 0 pushes air through inflow -> delivers reagent
    # into the now-empty well; pump 1 disposes drained waste to the waste port.
    t_b = time.monotonic()

    def _push_air():
        if air_backpad:
            _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
            _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", air_inc, slow)

    def _dispose_waste():
        _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", drain_inc_total, offchip)

    # Order matters: deliver the reagent into the drained well FIRST, then
    # dump pump 1's waste. The well is never left dry any longer than it was.
    _run_serial([_push_air, _dispose_waste])
    pump_log(f"COMMIT deliver+dispose done in {time.monotonic() - t_b:.1f}s")
    pump_log(f"COMMIT total in {time.monotonic() - t_start:.1f}s")


def dose(session: Session, reagent_name: str, *,
         record_min: float = 0.0, label: str | None = None,
         auto_wash: bool = True,
         require_swap: bool = True,
         volume_ul: float | None = None,
         air_backpad: bool = True,
         prep_next: str | None = None,
         skip_swap: bool = False) -> None:
    """Replace chip contents with ``reagent_name``, optionally recording.

    If ``auto_wash`` is true and the new reagent's class differs from the
    last source used, the syringe is automatically flushed with the wash
    reagent (typically PBS) first. Disable with ``auto_wash=False`` for
    cases where you explicitly want no wash (e.g., same-reagent re-doses).

    If ``require_swap`` is true (default), raise ``RuntimeError`` when the
    new reagent shares its valve port with the previous source but has a
    different name — that combination means a physical tube swap was needed
    and the script forgot to call ``swap_reagent_tube`` first. Pass
    ``require_swap=False`` only when you've manually verified the tube
    state. Reagents on dedicated ports (e.g., KA at port 3 vs aCSF at
    port 6) never trip this check.

    Phase markers are written to the event log (if recording) under
    ``label`` or ``"{reagent}_dose"``.

    ``prep_next``: if set, start a background prep for the named reagent
    AS SOON as this dose's recording starts. The prep does the syringe wash
    (if reagent class differs) and aspirates the next reagent into the
    syringe — both pump-0-only motions that don't touch the chip. When the
    next ``dose()`` is called for that reagent it skips the source-aspirate
    step (~30-60 s saved at the phase boundary). The recording duration is
    honored exactly; if prep hasn't finished by then, a NOTE is logged and
    the prep continues in the background — the next dose() call will block
    briefly for it to complete.

    ``skip_swap``: if true, do NOT replace chip contents — just record on
    whatever is already in the well, and (if ``prep_next``) pre-stage the
    next reagent during the recording. Used for the baseline phase that
    immediately follows ``swap_chip`` (the chip is already filled with the
    requested reagent, so re-running ``chip_swap`` would be wasted motion).

    Composes ``chip_swap`` + auto-wash + timed recording + pipelined
    ``prep_next`` into one call.
    """
    r = _get_reagent(session, reagent_name)

    # If a prep thread from a previous dose is still running, wait for it
    # to finish before committing (we need session.pending_prep to be up
    # to date so we know whether to use the pre-staged path).
    if session._prep_thread is not None and session._prep_thread.is_alive():
        pump_log(f"DOSE  {r.name}: waiting for in-flight prep thread to finish")
        session._prep_thread.join(timeout=120.0)
        if session._prep_thread.is_alive():
            pump_log(f"WARN: prep thread did not finish within 120s; proceeding with full chip_swap")

    # Tube-swap safety check: same port + different name means the user
    # needs to physically swap source tubing first. swap_reagent_tube()
    # sets last_source to the new reagent on completion, so a correctly
    # ordered script reaches this point with last_source.name == r.name
    # (same name, possibly same port → check passes).
    if (require_swap
            and session.last_source is not None
            and session.last_source.port == r.port
            and session.last_source.name != r.name):
        raise RuntimeError(
            f"dose({r.name!r}): the previous source was "
            f"{session.last_source.name!r} on the same port (port={r.port}), "
            f"so the physical tube must be swapped before dispensing "
            f"{r.name!r}. Call "
            f"swap_reagent_tube(current_reagent={session.last_source.name!r}, "
            f"next_reagent={r.name!r}) first. To bypass this check (e.g., "
            f"you've manually verified the tube swap), pass "
            f"require_swap=False."
        )

    # Pre-staged path: pump 0's syringe already holds this reagent thanks
    # to a prior dose(..., prep_next=r.name). Skip the source aspirate.
    use_prep = (
        session.pending_prep is not None
        and session.pending_prep.get("reagent_name") == r.name
    )

    # Stale-prep guard: a prior prep_next loaded the wrong reagent into the
    # syringe (script took an unexpected branch). Dispose to waste before
    # the fallback chip_swap so the next aspirate starts from an empty
    # plunger position.
    if session.pending_prep is not None and not use_prep:
        stale = session.pending_prep
        pump_log(
            f"DOSE  {r.name}: discarding stale prep ({stale['reagent_name']}, "
            f"{stale['fill_inc']} inc) to waste before fallback chip_swap"
        )
        waste_p0 = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="waste")
        _valve_to(session.url, DISPENSE_PUMP_ADDR, waste_p0)
        _plunger_move(
            session.url, DISPENSE_PUMP_ADDR, "dispense",
            stale["fill_inc"], session.offchip_speed_code,
        )
        session.pending_prep = None

    vol = float(volume_ul) if volume_ul is not None else session.working_volume_ul
    phase_name = label or f"dose_{r.name}"

    with _phase(session, phase_name):
        if skip_swap:
            pump_log(
                f"DOSE  {r.name} skip_swap=True — recording on existing well contents, "
                f"no chip motion"
            )
        elif use_prep:
            _commit_prestaged_dose(
                session, r,
                fill_inc=session.pending_prep["fill_inc"],
                air_inc=session.pending_prep["air_inc"],
                air_backpad=air_backpad,
            )
            session.pending_prep = None
        else:
            # Fallback / first dose: do the wash inline (slow) then full chip_swap.
            if auto_wash and session.last_source is not None and _should_auto_wash(session.last_source, r):
                pump_log(
                    f"AUTO-WASH: switching {session.last_source.name} ({session.last_source.role}) "
                    f"-> {r.name} ({r.role}) — flushing syringe"
                )
                wash_syringe(session)
            chip_swap(
                session,
                source_port=r.port,
                drain_volume_ul=vol,
                fill_volume_ul=vol,
                air_backpad=air_backpad,
            )

        session.last_source = r

        if record_min > 0:
            # Spawn the prep for the NEXT dose, if requested, as soon as
            # the recording starts — runs entirely in pump 0 land while we
            # sleep for record_min.
            if prep_next is not None:
                next_r = _get_reagent(session, prep_next)
                session.pending_prep = None  # clear any stale prep
                session._prep_thread = threading.Thread(
                    target=_prep_for_next,
                    args=(session, r, next_r),
                    daemon=True,
                )
                session._prep_thread.start()

            pump_log(f"RECORD start  {phase_name} for {record_min} min")
            time.sleep(record_min * 60)
            pump_log(f"RECORD done   {phase_name}")

            # Honor record_min exactly. If prep isn't done yet, log a note;
            # the next dose() will block briefly to let it finish.
            if (session._prep_thread is not None
                    and session._prep_thread.is_alive()):
                pump_log(
                    f"NOTE: prep_next={prep_next!r} still in progress at end of "
                    f"{record_min}min record (record shorter than prep)"
                )


def feed(session: Session, reagent_name: str, volume_ul: float, *,
         air_backpad: bool = True, auto_wash: bool = True) -> None:
    """Small-volume chip refresh for long-term cultures.

    Same mechanics as ``dose`` but typically smaller volume and without the
    recording sleep. Designed for "feed 50uL every 2 hours" patterns.

    A simpler dual-pump version of a media exchange, sized for
    chip-mounted samples rather than a full well swap.
    """
    pump_log(f"FEED   {reagent_name} {volume_ul:.1f}uL")
    dose(
        session,
        reagent_name,
        volume_ul=volume_ul,
        record_min=0,
        label=f"feed_{reagent_name}",
        auto_wash=auto_wash,
        air_backpad=air_backpad,
    )


def dry_chip(session: Session, *, air_volume_ul: float = 1000.0) -> None:
    """Push air through chip to drain + dry, alternating both pumps.

    Pump 0: load air from air port, push through chip-side line.
    Pump 1: aspirate from chip-side outflow to waste, removing anything
            being pushed through.
    The two alternate in small chunks rather than moving together — Habitat
    runs one motion at a time.

    Useful before swapping a faux chip out for a real one. Skips wells
    that already hold valuable sample — only run when chip is expendable.
    """
    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code
    air_inc = _ul_to_inc(air_volume_ul)
    air_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")
    waste_p1 = _find_port(session.pump_map[ASPIRATE_PUMP_ADDR], role="waste")

    pump_log(f"DRY    start  air_volume={air_volume_ul:.1f}uL ({air_inc} inc)")
    t_start = time.monotonic()

    # Stage 1: pump 0 loads a big slug of air, sequentially.
    _valve_to(url, DISPENSE_PUMP_ADDR, air_port)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)
    _valve_to(url, DISPENSE_PUMP_ADDR, session.chip_inflow_port)

    # Stage 2: sweep the air through the chip in AIR_SWEEP_CHUNK_UL chunks,
    # alternating pump 0's push with pump 1's draw. The two pumps can't move
    # at the same time any more (one job at a time), and pushing the whole
    # slug in before drawing any off would pressurize the well.
    _valve_to(url, ASPIRATE_PUMP_ADDR, session.chip_outflow_port)
    for chunk_inc in _split_increments(air_inc, AIR_SWEEP_CHUNK_UL):
        _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", chunk_inc, slow)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", chunk_inc, slow)
    _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
    _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", air_inc, offchip)
    pump_log(f"DRY    done   in {time.monotonic() - t_start:.1f}s")


def drain_chip(session: Session, *,
               drain_volume_ul: float | None = None,
               chip_outflow: int | None = None,
               outflow_ports: list[int] | None = None) -> None:
    """Drain the chip well to waste — no refill. Pump 1 only; pump 0 idle.

    Volume defaults to ``session.working_volume_ul`` (1× the well volume).
The reverse-creep compensation described above is applied automatically.

    Port selection, in precedence order:
        ``outflow_ports=[...]``  — sweep each port in turn, pulling the full
                                   volume through each. Pass
                                   ``chip_outflow_ports(session)`` on a
                                   four-port chip: the high port takes off
                                   everything above its height and the low
                                   port (listed last) pulls the chamber down
                                   to empty. Later ports mostly draw air once
                                   the level has dropped below them, which is
                                   harmless — it all goes to waste.
        ``chip_outflow=<port>``  — one explicit port.
        neither                  — ``session.chip_outflow_port``.

    Used by ``swap_chip`` (drain before manual chip change) and stand-alone
    when you just want to empty the well between phases.
    """
    pump1 = ASPIRATE_PUMP_ADDR
    if outflow_ports is not None:
        ports = list(outflow_ports)
    elif chip_outflow is not None:
        ports = [chip_outflow]
    else:
        ports = [session.chip_outflow_port]
    if not ports:
        raise ValueError("drain_chip: no outflow ports to drain through")
    waste_p1 = _find_port(session.pump_map[pump1], role="waste")

    vol = float(drain_volume_ul) if drain_volume_ul is not None else session.working_volume_ul
    drain_inc = _ul_to_inc(vol)
    overshoot_inc = _ul_to_inc(session.aspirate_overshoot_ul) if session.aspirate_overshoot_ul > 0 else 0
    drain_inc_total = drain_inc + overshoot_inc

    pump_log(
        f"DRAIN  start  vol={vol:.1f}uL ({drain_inc} inc) "
        f"chip_outflow={ports}@pump{pump1}"
        + (f" +overshoot={session.aspirate_overshoot_ul:.1f}uL" if overshoot_inc else "")
    )
    t_start = time.monotonic()
    for outflow in ports:
        if len(ports) > 1:
            pump_log(f"DRAIN  via chip_outflow={outflow}")
        _valve_to(session.url, pump1, outflow)
        _plunger_move(session.url, pump1, "aspirate", drain_inc_total, session.slow_speed_code)
        _valve_to(session.url, pump1, waste_p1)
        _plunger_move(session.url, pump1, "dispense", drain_inc_total, session.offchip_speed_code)
    pump_log(f"DRAIN  done   in {time.monotonic() - t_start:.1f}s")


def purge_chip_ports(session: Session, *,
                     air_volume_ul: float = 300.0,
                     inflow_ports: list[int] | None = None,
                     outflow_ports: list[int] | None = None,
                     drain_outflow: int | None = None) -> None:
    """Push air through every chip-side line on both pumps.

    Two passes:

        1. Inflow lines (pump 0). For each inflow, load an air slug and sweep
           it through the chip in ``AIR_SWEEP_CHUNK_UL`` chunks, alternating
           pump 0's push with a matching draw on pump 1 through
           ``drain_outflow``. Alternating is required, not cosmetic: Habitat
           runs one motion at a time, and pushing a whole slug in before
           drawing any off would pressurise the well.
        2. Outflow lines (pump 1). For each outflow, aspirate from the chip
           port and dispense to waste, clearing whatever that line still
           holds.

    Run this on an ALREADY-DRAINED well. Sweeping air through a full chamber
    blows bubbles into it — the same reason ``flush_chip`` defers its air
    purge until after every fill/drain cycle has finished.

    ``drain_outflow`` defaults to the LAST entry of the outflow list, which
    is the low port — the one that can keep up with the air being pushed in.
    """
    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code
    inflows = list(inflow_ports) if inflow_ports is not None else chip_inflow_ports(session)
    outflows = list(outflow_ports) if outflow_ports is not None else chip_outflow_ports(session)
    if not inflows or not outflows:
        raise ValueError(
            f"purge_chip_ports: need at least one inflow and one outflow "
            f"(got inflows={inflows}, outflows={outflows})"
        )
    drain = drain_outflow if drain_outflow is not None else outflows[-1]

    air_inc = _ul_to_inc(air_volume_ul)
    air_p0 = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")
    waste_p1 = _find_port(session.pump_map[ASPIRATE_PUMP_ADDR], role="waste")

    pump_log(
        f"PURGE  start  air={air_volume_ul:.0f}uL per line  "
        f"inflows={inflows} outflows={outflows} drain_via={drain}"
    )
    t_start = time.monotonic()

    for inflow in inflows:
        pump_log(f"PURGE  inflow port {inflow} (pump 0 push -> pump 1 draw via {drain})")
        _valve_to(url, DISPENSE_PUMP_ADDR, air_p0)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)
        _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
        _valve_to(url, ASPIRATE_PUMP_ADDR, drain)
        for chunk_inc in _split_increments(air_inc, AIR_SWEEP_CHUNK_UL):
            _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", chunk_inc, slow)
            _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", chunk_inc, slow)
        _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", air_inc, offchip)

    for outflow in outflows:
        pump_log(f"PURGE  outflow port {outflow} (pump 1 -> waste)")
        _valve_to(url, ASPIRATE_PUMP_ADDR, outflow)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", air_inc, slow)
        _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", air_inc, offchip)

    pump_log(f"PURGE  done   in {time.monotonic() - t_start:.1f}s")


def fill_chip(session: Session, reagent_name: str, *,
              volume_ul: float | None = None,
              in_port: int | None = None,
              air_backpad_ul: float | None = None,
              label: str | None = None) -> None:
    """Dispense ``volume_ul`` of reagent into the chip through one inlet,
    optionally chased by an air backpad. Pump 0 only; pump 1 idle.

    Reagent and air are drawn in ONE load (reagent first, air on top of it)
    so the air comes out behind the reagent and clears the chip-side line.
    The syringe ends empty either way.

    Use on an EMPTY chamber — this only adds. ``exchange_media`` is the one
    that takes fluid out first; ``dose`` is the one that swaps the whole well.

    ``air_backpad_ul`` defaults to ``session.air_backpad_ul``; pass 0.0 to
    leave reagent standing in the chip-side line instead (worth doing when
    the next operation will dispense through the same inlet anyway).
    """
    r = _get_reagent(session, reagent_name)
    vol = float(volume_ul) if volume_ul is not None else session.working_volume_ul
    inp = in_port if in_port is not None else session.chip_inflow_port
    air_ul = float(air_backpad_ul) if air_backpad_ul is not None else session.air_backpad_ul

    url = session.url
    fill_inc = _ul_to_inc(vol)
    air_inc = _ul_to_inc(air_ul) if air_ul > 0 else 0

    with _phase(session, label or f"fill_{r.name}"):
        pump_log(
            f"FILL   start  {r.name} {vol:.1f}uL ({fill_inc} inc) -> "
            f"chip_inflow={inp}@pump{DISPENSE_PUMP_ADDR}"
            + (f" + {air_ul:.0f}uL air backpad" if air_inc else " (no air backpad)")
        )
        t_start = time.monotonic()

        _valve_to(url, DISPENSE_PUMP_ADDR, r.port)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", fill_inc, session.offchip_speed_code)
        if air_inc:
            air_p0 = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")
            _valve_to(url, DISPENSE_PUMP_ADDR, air_p0)
            _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, session.offchip_speed_code)

        _valve_to(url, DISPENSE_PUMP_ADDR, inp)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", fill_inc, session.slow_speed_code)
        if air_inc:
            _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", air_inc, session.slow_speed_code)

        session.last_source = r
        pump_log(f"FILL   done   in {time.monotonic() - t_start:.1f}s")


def exchange_media(session: Session, reagent_name: str, *,
                   volume_ul: float | None = None,
                   out_port: int | None = None,
                   in_port: int | None = None,
                   air_backpad_ul: float = 0.0,
                   apply_overshoot: bool = False,
                   label: str | None = None) -> None:
    """Partial media exchange: take ``volume_ul`` out of the well, put the
    same volume of fresh reagent back in. The well level is unchanged at the
    end; only the fluid is newer.

    Removal happens first, so the chamber has headroom before fresh media
    arrives and the level never overshoots the high port.

    Unlike ``dose``, this does NOT swap the whole well, does not wash the
    syringe, and defaults to NO air backpad. On a single-reagent rig there is
    nothing to cross-contaminate, so leaving media standing in the chip-side
    line between exchanges is free — and it means the next exchange delivers
    immediately instead of spending its first ~100 uL re-wetting the line.
    Pushing a backpad through an inflow on a well that is near-full is also
    how you get bubbles in the chamber. Pass ``air_backpad_ul`` to override.

    ``apply_overshoot`` is off by default, again unlike ``dose`` / ``drain_chip``.
    That compensation deliberately over-aspirates, which is right when
    the goal is "empty the well" and wrong when the goal is "remove exactly
    this many uL".

    NOTE on ``out_port``: pulling a metered volume through a HIGH outflow only
    moves liquid while the chamber level is above that port. Once it drops
    below, the pump happily aspirates air and reports success — the motion is
    identical either way. The volume logged here is the volume requested, not
    the volume of liquid that actually moved.
    """
    r = _get_reagent(session, reagent_name)
    vol = float(volume_ul) if volume_ul is not None else session.working_volume_ul
    outp = out_port if out_port is not None else session.chip_outflow_port
    inp = in_port if in_port is not None else session.chip_inflow_port

    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code
    waste_p1 = _find_port(session.pump_map[ASPIRATE_PUMP_ADDR], role="waste")

    vol_inc = _ul_to_inc(vol)
    overshoot_inc = (
        _ul_to_inc(session.aspirate_overshoot_ul)
        if apply_overshoot and session.aspirate_overshoot_ul > 0 else 0
    )
    remove_inc = vol_inc + overshoot_inc
    air_inc = _ul_to_inc(air_backpad_ul) if air_backpad_ul > 0 else 0

    with _phase(session, label or f"exchange_{r.name}"):
        pump_log(
            f"EXCH   start  {r.name} {vol:.1f}uL ({vol_inc} inc)  "
            f"out=port {outp}@pump{ASPIRATE_PUMP_ADDR} -> in=port {inp}@pump{DISPENSE_PUMP_ADDR}"
            + (f"  +overshoot={session.aspirate_overshoot_ul:.1f}uL" if overshoot_inc else "")
            + (f"  air_backpad={air_backpad_ul:.0f}uL" if air_inc else "")
        )
        t_start = time.monotonic()

        # 1. Remove spent media from the well -> waste.
        _valve_to(url, ASPIRATE_PUMP_ADDR, outp)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", remove_inc, slow)
        _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", remove_inc, offchip)

        # 2. Draw the same volume of fresh reagent and deliver it to the well.
        #    The air backpad, when enabled, is loaded on top of the reagent so
        #    it follows it out of the syringe and clears the chip-side line.
        _valve_to(url, DISPENSE_PUMP_ADDR, r.port)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", vol_inc, offchip)
        if air_inc:
            air_p0 = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")
            _valve_to(url, DISPENSE_PUMP_ADDR, air_p0)
            _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)
        _valve_to(url, DISPENSE_PUMP_ADDR, inp)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", vol_inc, slow)
        if air_inc:
            _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", air_inc, slow)

        session.last_source = r
        pump_log(f"EXCH   done   in {time.monotonic() - t_start:.1f}s")


def swap_chip(session: Session, *,
              fill_reagent: str = "aCSF",
              prompt: bool = True,
              prefill_volume_ul: float | None = None) -> None:
    """Drain the chip, pre-fill the dispense line with fresh reagent, pause for
    the manual chip swap, then push the pre-filled reagent into the new chip.

    Workflow:
        1. drain_chip(session) — empty the current well to waste.
        2. Pre-fill pump 0's chip-side line with ``fill_reagent``:
              valve -> source, aspirate (prefill_volume + working_volume)
              valve -> chip_inflow, dispense the prefill volume
              valve -> air, aspirate air_backpad
           After this step the dispense tube tip is wet with fill_reagent
           (so reconnecting the new chip sees liquid immediately instead of
           sitting dry until ``dose()``'s aspirate finishes), and the
           syringe holds working_volume reagent + air_backpad air ready
           for the final delivery.
        3. If ``prompt``: input("Swap chip, press Enter when done").
        4. Commit: pump 0 dispenses the reagent through the chip inflow
           (line content) then the air backpad — delivering working_volume
           into the new chip in ~15 s instead of the previous ~60+ s.

    ``prefill_volume_ul`` defaults to ``working_volume_ul``; bump it up if
    the chip-side line dead volume is larger than one working volume.
    """
    pump_log(f"CHIP-SWAP start fill_reagent={fill_reagent}")
    t_start = time.monotonic()

    r = _get_reagent(session, fill_reagent)
    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code
    inflow = session.chip_inflow_port

    prefill_ul = float(prefill_volume_ul) if prefill_volume_ul is not None else session.working_volume_ul
    prefill_inc = _ul_to_inc(prefill_ul)
    air_inc = _ul_to_inc(session.air_backpad_ul)
    air_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")

    # 1. Drain old chip.
    drain_chip(session)

    # 2. Pre-fill chip-side line with fresh reagent so reconnecting the
    #    new chip sees liquid at the tube tip, then load the syringe with
    #    JUST the air backpad. The chip-side line now holds prefill_ul of
    #    reagent at its pump end; commit (step 4) will push it into the
    #    new chip with the air backpad. Aspirating only what we dispense
    #    keeps the syringe clean (no reagent left behind to contaminate
    #    the next aspirate).
    pump_log(
        f"CHIP-SWAP pre-fill: aspirate {prefill_ul:.0f}uL {r.name}, dispense all into "
        f"chip_inflow={inflow} (tube tip wet), then aspirate {session.air_backpad_ul:.0f}uL air"
    )
    _valve_to(url, DISPENSE_PUMP_ADDR, r.port)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", prefill_inc, offchip)
    _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", prefill_inc, slow)
    _valve_to(url, DISPENSE_PUMP_ADDR, air_port)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)

    # 3. User intervention — chip-side tube tip is wet, syringe loaded with
    #    air only (no leftover reagent to contaminate later operations).
    if prompt:
        _wait_for_enter("\n>>> Swap the chip now. Press Enter when the new chip is connected...\n")

    # 4. Commit: dispense the air backpad through chip_inflow — pushes the
    #    pre-filled reagent in the line into the new chip well, then air
    #    follows. Syringe ends empty.
    pump_log(f"CHIP-SWAP commit: push air backpad to deliver pre-filled {r.name} into new chip")
    t_commit = time.monotonic()
    _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
    _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", air_inc, slow)
    session.last_source = r
    pump_log(f"CHIP-SWAP commit done in {time.monotonic() - t_commit:.1f}s")

    pump_log(f"CHIP-SWAP done in {time.monotonic() - t_start:.1f}s")


def flush_chip(session: Session, *,
               with_reagent: str = "PBS",
               cycles: int = 3,
               volume_ul: float = 500.0,
               inflow_ports: list[int] | None = None,
               outflow_port: int | None = None,
               final_air_purge: bool = True,
               final_air_per_port_ul: float = 200.0,
               final_mop_up_ul: float = 300.0,
               leave_filled: bool = False) -> None:
    """Flush the chip flow-path with ``with_reagent`` to clear residual
    chemistry (e.g., the 10%% H2O2 sterilization wash before a slice chip
    is loaded).

    A pre-prime stage wets the chip-side inflow lines once (so cycle-1
    dispense isn't eaten by line dead volume), then each cycle fills
    the well to ~``volume_ul`` SEQUENTIALLY before draining. Sequential
    fill→drain is what guarantees the well climbs high enough to slosh
    PBS up the chamber sides; pipelined parallel drain would keep the
    well at ~100uL equilibrium and defeat the chamber-wash goal.

    No per-cycle air backpad — pushing air through the chip-low inflow
    while the well is full creates bubbles in the well. The final air
    purge (drain → sweep each line) runs once after all cycles complete,
    with the well in an empty/drained state so no bubbles form.

        Pre-prime (once, no drain):
            valve -> source, aspirate ``volume_ul``
            for each inflow port in order:
                valve -> inflow, dispense (volume_ul / N)
            -> chip-side lines now wet; well at ~half ``volume_ul``.

        Per cycle (sequential pump 0 fill -> pump 1 drain):
            pump 0:
                valve -> source, aspirate ``volume_ul``
                for each inflow port in order:
                    valve -> inflow, dispense (volume_ul / N)
                -> well climbs to ~``volume_ul`` (chamber-side wash).
            pump 1:
                valve -> outflow, aspirate ``volume_ul`` (+ overshoot)
                valve -> waste, dispense same

        Final (only if ``final_air_purge``):
            (1) For each inflow port: pump 0 aspirates air and sweeps
                ``final_air_per_port_ul`` through the inflow line, with
                pump 1 draining the chip in parallel so the swept liquid
                + air exits cleanly to waste.
            (2) Mop-up: pump 1 aspirates ``final_mop_up_ul`` from the chip
                outflow (pulling residual liquid + air) and disposes to
                waste, leaving the well as dry as the outflow geometry
                allows. Set to 0 to skip.

    ``inflow_ports=None`` → ``chip_inflow_ports(session)``: the names from
    ``session.chip_ports`` when configured, otherwise every ``role="chip"``
    entry on the dispense pump in port-number order.

    Draining uses ONE outflow for every cycle — ``outflow_port``, defaulting
    to ``session.chip_outflow_port`` (the low port on a four-port chip). That
    is deliberate: the low port empties the chamber on its own, and sweeping
    both outflows every cycle would double the drain time for nothing. Use
    ``drain_chip(outflow_ports=chip_outflow_ports(session))`` when you do want
    both lines cleared.
    """
    if inflow_ports is None:
        inflow_ports = chip_inflow_ports(session)
    if not inflow_ports:
        raise ValueError("flush_chip: no chip-role ports on dispense pump; specify inflow_ports.")

    r = _get_reagent(session, with_reagent)
    url = session.url
    slow = session.slow_speed_code
    offchip = session.offchip_speed_code

    fill_inc = _ul_to_inc(volume_ul)
    n_ports = len(inflow_ports)
    fill_inc_per_port = fill_inc // n_ports  # integer split — any remainder dropped (sub-uL)

    # Reverse-creep compensation on pump 1's drain (see above).
    overshoot_inc = _ul_to_inc(session.aspirate_overshoot_ul) if session.aspirate_overshoot_ul > 0 else 0
    drain_inc_total = fill_inc + overshoot_inc

    waste_p1 = _find_port(session.pump_map[ASPIRATE_PUMP_ADDR], role="waste")
    outflow = outflow_port if outflow_port is not None else session.chip_outflow_port

    pump_log(
        f"FLUSH  start  with={r.name} (port {r.port}) inflow_ports={inflow_ports} "
        f"cycles={cycles} volume={volume_ul:.1f}uL/cycle split {fill_inc_per_port} inc x {n_ports} "
        f"final_air_purge={final_air_purge}"
    )
    t_start = time.monotonic()

    def _do_fill():
        _valve_to(url, DISPENSE_PUMP_ADDR, int(r.port))
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", fill_inc, offchip)
        for inflow in inflow_ports:
            _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
            _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", fill_inc_per_port, slow)

    def _do_drain():
        _valve_to(url, ASPIRATE_PUMP_ADDR, outflow)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", drain_inc_total, slow)
        _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
        _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", drain_inc_total, offchip)

    # Pre-prime: one fill with no drain so chip-side inflow lines are wet
    # before the cycles begin. Without this, cycle-1's dispense is mostly
    # consumed by line dead volume and the well never reaches volume_ul.
    pump_log(f"FLUSH  pre-prime: wetting inflow lines (no drain)")
    t_pre = time.monotonic()
    _do_fill()
    pump_log(f"FLUSH  pre-prime done in {time.monotonic() - t_pre:.1f}s")

    # Sequential cycles: pump 0 fills well to ~volume_ul, then pump 1
    # drains. Sequential is the point — pipelined parallel drain would
    # keep the well at low equilibrium and skip the chamber-side wash.
    for i in range(cycles):
        pump_log(f"FLUSH  cycle {i+1}/{cycles}")
        t_cycle = time.monotonic()
        _do_fill()
        pump_log(f"FLUSH  cycle {i+1} fill done in {time.monotonic() - t_cycle:.1f}s")
        t_drain = time.monotonic()
        _do_drain()
        pump_log(f"FLUSH  cycle {i+1} drain done in {time.monotonic() - t_drain:.1f}s")
        pump_log(f"FLUSH  cycle {i+1} total in {time.monotonic() - t_cycle:.1f}s")

    # Leave-filled mode (e.g. for sleep): one extra fill with no drain so
    # the chip sits in ~volume_ul of the reagent. Skips the final air purge
    # because we WANT liquid in the chip (and lines).
    if leave_filled:
        pump_log(f"FLUSH  leave_filled: one extra fill so chip holds ~{volume_ul:.0f}uL {r.name}")
        t_fill = time.monotonic()
        _do_fill()
        pump_log(f"FLUSH  final fill done in {time.monotonic() - t_fill:.1f}s")
        session.last_source = r
        pump_log(f"FLUSH  done   in {time.monotonic() - t_start:.1f}s")
        return

    # Final air purge: drain chip empty, then sweep each inflow line with
    # air. Doing this AFTER the last cycle (rather than per-cycle) keeps
    # the chip full of fresh PBS during the cleaning cycles, and the well
    # is drained before any air is pushed so no bubbles form in liquid.
    if final_air_purge:
        air_inc_per_port = _ul_to_inc(final_air_per_port_ul)
        air_inc = air_inc_per_port * n_ports
        air_port = _find_port(session.pump_map[DISPENSE_PUMP_ADDR], role="air")

        pump_log(
            f"FLUSH  final-purge: sweep {final_air_per_port_ul:.0f}uL air through each of "
            f"{inflow_ports} ({air_inc} inc total) — well already empty"
        )
        t_purge = time.monotonic()

        # The well is already empty after the cycle-N final drain above.
        # Aspirate enough air for the full sweep, then dispense through each
        # inflow line in turn, alternating with pump 1's draw in small chunks
        # so anything pushed into the empty well exits promptly to waste.
        _valve_to(url, DISPENSE_PUMP_ADDR, air_port)
        _plunger_move(url, DISPENSE_PUMP_ADDR, "aspirate", air_inc, offchip)

        for inflow in inflow_ports:
            t_p = time.monotonic()
            _valve_to(url, DISPENSE_PUMP_ADDR, inflow)
            _valve_to(url, ASPIRATE_PUMP_ADDR, outflow)
            for chunk_inc in _split_increments(air_inc_per_port, AIR_SWEEP_CHUNK_UL):
                _plunger_move(url, DISPENSE_PUMP_ADDR, "dispense", chunk_inc, slow)
                _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", chunk_inc, slow)
            _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
            _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", air_inc_per_port, offchip)
            pump_log(f"FLUSH  final-purge inflow={inflow} done in {time.monotonic() - t_p:.1f}s")

        pump_log(f"FLUSH  final-purge total in {time.monotonic() - t_purge:.1f}s")

        # Mop-up: pull any residual liquid the inflow-side air-push pushed
        # into the well that the parallel drain didn't catch.
        if final_mop_up_ul > 0:
            mop_inc = _ul_to_inc(final_mop_up_ul)
            pump_log(f"FLUSH  mop-up: aspirate {final_mop_up_ul:.0f}uL from chip outflow")
            t_mop = time.monotonic()
            _valve_to(url, ASPIRATE_PUMP_ADDR, outflow)
            _plunger_move(url, ASPIRATE_PUMP_ADDR, "aspirate", mop_inc, slow)
            _valve_to(url, ASPIRATE_PUMP_ADDR, waste_p1)
            _plunger_move(url, ASPIRATE_PUMP_ADDR, "dispense", mop_inc, offchip)
            pump_log(f"FLUSH  mop-up done in {time.monotonic() - t_mop:.1f}s")

    session.last_source = r
    pump_log(f"FLUSH  done   in {time.monotonic() - t_start:.1f}s")


def clean_chip(session: Session, with_reagent: str = "aCSF", *,
               cycles: int | None = None, dry: bool = False) -> None:
    """Flow ``with_reagent`` through the chip ``cycles`` times, then optionally
    dry it down. Used to clear residual chemistry (e.g., peroxide) before a
    real chip is loaded.

    If ``cycles`` is None, defaults to ``session.chip_wash_cycles``.
    """
    n = cycles if cycles is not None else session.chip_wash_cycles
    pump_log(f"CLEAN  start  with={with_reagent} cycles={n} dry={dry}")
    t_start = time.monotonic()
    for i in range(n):
        # Don't auto-wash within a multi-cycle clean — we're flowing the same
        # reagent repeatedly on purpose.
        dose(
            session, with_reagent,
            label=f"clean_{with_reagent}_{i+1}",
            auto_wash=(i == 0),
        )
    if dry:
        dry_chip(session)
    pump_log(f"CLEAN  done   in {time.monotonic() - t_start:.1f}s")


def initialize_reagents(session: Session, order: list[str], *,
                        prime_cycles: int = 1, prime_volume_ul: float | None = None) -> None:
    """Prime each reagent's source line in ``order``. Auto-inserts wash
    cycles between reagents whose roles differ (per ``_should_auto_wash``).

    Typical usage: ``initialize_reagents(session, ["KA", "olanzapine_low",
    "olanzapine_mid", "olanzapine_high", "aCSF"])`` — drugs first, PBS
    wash auto-injected between the last drug and aCSF.
    """
    pump_log(f"INIT   reagents in order: {order}")
    t_start = time.monotonic()
    last: Reagent | None = None
    for name in order:
        r = _get_reagent(session, name)
        if last is not None and _should_auto_wash(last, r):
            wash_syringe(session)
        prime(session, name, cycles=prime_cycles, volume_ul=prime_volume_ul)
        last = r
    # The last reagent primed is now "what's in the syringe / line." Reflect
    # that on the session so a following dose() can skip a redundant wash.
    session.last_source = last
    pump_log(f"INIT   done   in {time.monotonic() - t_start:.1f}s")


# =============================================================================
# Recording lifecycle
# =============================================================================


def begin_recording(session: Session, events_path: str) -> None:
    """Open the event log and start the outer ``recording`` phase.

    All ``dose`` / ``feed`` / etc. calls that follow will write phase markers
    to this log until ``end_recording`` is called.

    NOTE: this only manages the FLUIDIC event log. MaxOne recording itself
    (the .raw.h5 capture) is started separately — see run_protocol.py for
    the MaxLab integration. Once Habitat owns this, recording start/stop
    should hook into the same lifecycle.
    """
    os.makedirs(os.path.dirname(events_path) or ".", exist_ok=True)
    session._events_file = open(events_path, "w")
    session._t0_monotonic = time.monotonic()
    session._outer_phase_cm = _phase(session, "recording")
    session._outer_phase_cm.__enter__()
    pump_log(f"RECORDING start -> {events_path}")


def end_recording(session: Session) -> None:
    """Close the outer ``recording`` phase and flush the event log."""
    if session._outer_phase_cm is not None:
        session._outer_phase_cm.__exit__(None, None, None)
        session._outer_phase_cm = None
    if session._events_file is not None:
        session._events_file.close()
        session._events_file = None
    session._t0_monotonic = None
    pump_log("RECORDING done")
