#!/usr/bin/env python3
"""
infra_cc/full_setup.py
Tiered orchestrator for the end-to-end setup/teardown.

Tiers:
  up:
    - network : VPC + subnets + IGW
    - routing : network + NAT + route tables (private default -> NAT, main default -> IGW)
    - ec2     : routing + 2 EC2 instances (public & private)

  down (cascading intent):
    - ec2     : terminate instances (safe to run anytime)
    - routing : delete private RT + delete NAT (asks ARE YOU SURE?)
    - network : routing + ec2 + delete VPC (asks ARE YOU SURE? if NAT present)

Usage:
  python -m infra_cc.full_setup up   --tier network|routing|ec2
  python -m infra_cc.full_setup down --tier ec2|routing|network [--purge]
  python -m infra_cc.full_setup status
"""

from __future__ import annotations
import argparse, sys, time, threading
from botocore.exceptions import ClientError

from . import vpc, subnet, igw, natgw, routes, ec2nodes
from .session import ec2

# ------------------ Config: seal threshold ------------------
# Show the filled "[^^^^^^^]" line ONLY if a step (or wait) took at least this many seconds.
_SEAL_THRESHOLD_SECONDS = 300

# ------------------ ASCII spinner + timers ------------------

# ASCII "comet" frames; prompt mode renders as asterisks with a helpful message.
_COMET_FRAMES = [
    "[>      ]", "[>>     ]", "[>>>    ]", "[ >>>   ]", "[  >>>  ]",
    "[   >>> ]", "[    >>>]", "[     >>]", "[      >]", "[       ]",
]
_FILLED_FRAME = "[^^^^^^^]"  # shown when sealing a long step

_spinner_stop = threading.Event()
_spinner_lock = threading.Lock()
_spinner_thread: threading.Thread | None = None
_spinner_tls = threading.local()   # lets spinner bypass stdout proxy

_overall_start = 0.0
_current_task = "starting…"
_task_start = 0.0
_spinner_prompt = False
_prompt_line_drawn = False
_spinner_prompt_msg = ""           # shows exactly what the user can type (e.g., "Enter Y or N")
_seal_pending = False              # next foreign print should seal/clear current spinner line first

# ----- stdout proxy so ANY print seals/clears spinner line first -----

_stdout_real = sys.stdout
_stdout_proxy = None

def _fmt_elapsed(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"

def _clear_spinner_line():
    _spinner_tls.writing = True
    try:
        _stdout_real.write("\r" + " " * 90 + "\r")
        _stdout_real.flush()
    finally:
        _spinner_tls.writing = False

def _seal_or_clear_current_line(per_seconds: float):
    """If long step (>= threshold) print a filled line; else just clear spinner line."""
    global _seal_pending
    overall = _fmt_elapsed(time.time() - _overall_start)
    if per_seconds >= _SEAL_THRESHOLD_SECONDS:
        per = _fmt_elapsed(per_seconds)
        _spinner_tls.writing = True
        try:
            _stdout_real.write(f"\r{_FILLED_FRAME} overall {overall} | current: {_current_task} {per}\n")
            _stdout_real.flush()
        finally:
            _spinner_tls.writing = False
    else:
        _clear_spinner_line()
    with _spinner_lock:
        _seal_pending = False

class _StdoutProxy:
    def __init__(self, real):
        self._real = real

    def write(self, s):
        # If spinner thread is writing, or spinner inactive, just pass through
        if getattr(_spinner_tls, "writing", False) or not _spinner_thread or not _spinner_thread.is_alive():
            return self._real.write(s)

        # Avoid interference while prompting (asterisk line is managed elsewhere)
        if _spinner_prompt:
            return self._real.write(s)

        # If a spinner frame was drawn since last seal, seal/clear before printing foreign output
        global _seal_pending
        do_seal = False
        with _spinner_lock:
            if _seal_pending:
                do_seal = True
                per_seconds = time.time() - _task_start
            # else: no need to adjust the spinner line

        if do_seal:
            _seal_or_clear_current_line(per_seconds)

        return self._real.write(s)

    def flush(self):
        return self._real.flush()

def _stdout_patch_start():
    global _stdout_proxy, _stdout_real
    if sys.stdout is _stdout_real:
        _stdout_proxy = _StdoutProxy(_stdout_real)
        sys.stdout = _stdout_proxy

def _stdout_patch_stop():
    global _stdout_real
    if sys.stdout is not _stdout_real:
        sys.stdout = _stdout_real

# ----- spinner core -----

def _spinner_loop():
    global _prompt_line_drawn, _seal_pending
    i = 0
    while not _spinner_stop.is_set():
        with _spinner_lock:
            overall = _fmt_elapsed(time.time() - _overall_start)
            per = _fmt_elapsed(time.time() - _task_start)
            task = _current_task
            prompt = _spinner_prompt
            prompt_msg = _spinner_prompt_msg

        if prompt:
            if not _prompt_line_drawn:
                _spinner_tls.writing = True
                try:
                    _stdout_real.write(f"\r[*******] overall {overall} | {prompt_msg}\n")
                    _stdout_real.flush()
                finally:
                    _spinner_tls.writing = False
                _prompt_line_drawn = True
            time.sleep(0.2)
            continue

        _prompt_line_drawn = False
        frame = _COMET_FRAMES[i % len(_COMET_FRAMES)]
        line = f"\r{frame} overall {overall} | current: {task} {per}"
        _spinner_tls.writing = True
        try:
            _stdout_real.write(line)
            _stdout_real.flush()
        finally:
            _spinner_tls.writing = False

        with _spinner_lock:
            _seal_pending = True

        time.sleep(0.1)
        i += 1

    # clear the line on stop
    _clear_spinner_line()

def _spinner_start():
    global _spinner_thread, _overall_start, _task_start, _current_task
    global _spinner_prompt, _prompt_line_drawn, _spinner_prompt_msg, _seal_pending
    _overall_start = time.time()
    _task_start = _overall_start
    _current_task = "starting…"
    _spinner_prompt = False
    _prompt_line_drawn = False
    _spinner_prompt_msg = ""
    _seal_pending = False
    _spinner_stop.clear()
    _stdout_patch_start()
    _spinner_thread = threading.Thread(target=_spinner_loop, daemon=True)
    _spinner_thread.start()

def _spinner_set_task(desc: str):
    global _task_start, _current_task
    with _spinner_lock:
        _current_task = desc
        _task_start = time.time()
        # no need to flip _seal_pending; first frame of new task will set it

def _spinner_set_prompt(on: bool, msg: str = "Waiting for input…"):
    global _spinner_prompt, _spinner_prompt_msg, _prompt_line_drawn, _seal_pending
    with _spinner_lock:
        _spinner_prompt = on
        _spinner_prompt_msg = msg
        _prompt_line_drawn = False
        _seal_pending = False

def _spinner_stop_now():
    _spinner_stop.set()
    if _spinner_thread:
        _spinner_thread.join(timeout=2.0)
    _stdout_patch_stop()

def _print_finish_line_if_long(desc: str, per_seconds: float):
    """Show the filled caret line only if the step exceeded the threshold; otherwise clear."""
    overall = _fmt_elapsed(time.time() - _overall_start)
    if per_seconds >= _SEAL_THRESHOLD_SECONDS:
        per = _fmt_elapsed(per_seconds)
        _spinner_tls.writing = True
        try:
            _stdout_real.write(f"\r{_FILLED_FRAME} overall {overall} | finished: {desc} {per}\n")
            _stdout_real.flush()
        finally:
            _spinner_tls.writing = False
    else:
        _clear_spinner_line()
    with _spinner_lock:
        global _seal_pending
        _seal_pending = False

def _run_step(desc: str, fn, *args, **kwargs):
    """Set spinner task, run fn, then (conditionally) show a filled line and always show [done]."""
    _spinner_set_task(desc)
    t0 = time.time()
    result = fn(*args, **kwargs)
    per_seconds = time.time() - t0
    _print_finish_line_if_long(desc, per_seconds)
    print(f"[done] {desc} in {_fmt_elapsed(per_seconds)}")
    return result

def _spin_until(desc: str, predicate, timeout: int = 900, poll: float = 1.5) -> bool:
    """Keep spinner going while we wait for predicate() to become True."""
    _spinner_set_task(desc)
    start = time.time()
    while True:
        if predicate():
            per = time.time() - start
            _print_finish_line_if_long(desc, per)
            print(f"[done] {desc} in {_fmt_elapsed(per)}")
            return True
        if time.time() - start >= timeout:
            per = time.time() - start
            _print_finish_line_if_long(desc, per)
            print(f"[warn] timeout waiting for: {desc} after {_fmt_elapsed(per)}")
            return False
        time.sleep(poll)

# ------------------ tiny AWS helpers ------------------

def _nat_exists() -> bool:
    c = ec2()
    try:
        resp = c.describe_nat_gateways(
            Filters=[
                {"Name": "tag:Name", "Values": [natgw.NATGW_NAME]},
                {"Name": "state", "Values": ["pending", "available", "deleting"]},
            ]
        )
        return bool(resp.get("NatGateways"))
    except ClientError:
        return False

def _any_instances_in_vpc() -> bool:
    from . import vpc as vpc_mod
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        return False
    res = c.describe_instances(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "instance-state-name", "Values": ["pending","running","stopping","stopped"]},
        ]
    ).get("Reservations", [])
    inst = [i for r in res for i in r.get("Instances", [])]
    return bool(inst)

def _vpc_exists() -> bool:
    from . import vpc as vpc_mod
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        return False
    try:
        c.describe_vpcs(VpcIds=[vpc_id])
        return True
    except ClientError:
        return False

# ------------------ NAT delete confirmations ------------------

def _confirm_nat_delete(context: str) -> None:
    """Show big banner and Y/N prompt; spinner switches to asterisk bar with explicit inputs."""
    if not _nat_exists():
        return
    print("")
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print("  WARNING: This operation will DELETE the NAT Gateway (and release EIP).")
    print(f"  Context: {context}")
    print("  NAT deletes can take a while; proceed only if you intend to remove it.")
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    _spinner_set_prompt(True, "Confirm NAT deletion — enter Y or N")
    try:
        ans = input("ARE YOU SURE? (Y/N): ").strip().lower()
    finally:
        _spinner_set_prompt(False)
    if ans not in ("y", "yes"):
        raise SystemExit("[abort] NAT deletion cancelled by user")

def _nat_delete_countdown(seconds: int = 60, skippable: bool = True):
    """
    After Y/N confirmation, give a final visible countdown that the user can:
      - **Enter** to skip and proceed immediately
      - **Ctrl+C** to cancel
    Spinner is paused (asterisk line) to avoid interference.
    """
    if not _nat_exists():
        return
    width = 40
    _spinner_set_prompt(True, f"NAT deletion starts in {seconds}s — Press Enter to skip, Ctrl+C to cancel")
    print("NAT deletion will start automatically. Press Enter to skip; Ctrl+C to cancel.")
    try:
        import select
        start = time.time()
        while True:
            elapsed = int(time.time() - start)
            remaining = max(seconds - elapsed, 0)
            filled = int(((seconds - remaining) / seconds) * width)
            bar = "#" * filled + "-" * (width - filled)
            _stdout_real.write(f"\r[NAT COUNTDOWN] T-{remaining:02d}s | {bar}")
            _stdout_real.flush()
            if remaining <= 0:
                break
            if skippable:
                rlist, _, _ = select.select([sys.stdin], [], [], 1.0)
                if rlist:
                    _ = sys.stdin.readline()
                    _stdout_real.write("\n[skip] Countdown skipped by user; proceeding with NAT deletion.\n")
                    _stdout_real.flush()
                    break
            else:
                time.sleep(1.0)
        _stdout_real.write("\n")
        _stdout_real.flush()
    except KeyboardInterrupt:
        _stdout_real.write("\n[abort] NAT deletion cancelled by user\n")
        _stdout_real.flush()
        _spinner_set_prompt(False)
        raise SystemExit("[abort] NAT deletion cancelled by user")
    finally:
        _spinner_set_prompt(False)

# ------------------ Bring UP ------------------

def up_network():
    _spinner_start()
    try:
        _run_step("vpc.create", vpc.create)
        _run_step("subnet.create(both)", subnet.create, "both")
        _run_step("igw.create_attach", igw.create_attach)
    finally:
        _spinner_stop_now()

def up_routing():
    _spinner_start()
    try:
        _run_step("vpc.create", vpc.create)
        _run_step("subnet.create(both)", subnet.create, "both")
        _run_step("igw.create_attach", igw.create_attach)

        _run_step("natgw.create", natgw.create)                 # natgw.py has its own wait spinner; we still wrap for timing
        _run_step("routes.create_private", routes.create_private)
        _run_step("routes.set_private_default", routes.set_private_default)
        _run_step("routes.set_public_main", routes.set_public_main)
    finally:
        _spinner_stop_now()

def up_ec2():
    _spinner_start()
    try:
        # lower tiers, idempotent
        _run_step("vpc.create", vpc.create)
        _run_step("subnet.create(both)", subnet.create, "both")
        _run_step("igw.create_attach", igw.create_attach)
        _run_step("natgw.create", natgw.create)
        _run_step("routes.create_private", routes.create_private)
        _run_step("routes.set_private_default", routes.set_private_default)
        _run_step("routes.set_public_main", routes.set_public_main)
        # ec2
        _run_step("ec2nodes.create(both)", ec2nodes.create, "both")
    finally:
        _spinner_stop_now()

def status():
    # no spinner needed for quick reads
    vpc.status()
    subnet.status("both")
    igw.status()
    natgw.status()
    routes.status()
    ec2nodes.status("both")

# ------------------ Tear DOWN (cascading + spinner) ------------------

def down_ec2(purge: bool):
    _spinner_start()
    try:
        _run_step("ec2nodes.delete(both)", ec2nodes.delete, "both", purge=purge)
        # Wait until no instances remain (handles ENI lag)
        _spin_until("waiting: EC2 instances gone", lambda: not _any_instances_in_vpc(), timeout=1200, poll=2.0)
    finally:
        _spinner_stop_now()

def down_routing():
    _spinner_start()
    try:
        # ensure EC2 is gone first (so ENIs don't block NAT/subnet)
        if _any_instances_in_vpc():
            _run_step("ec2nodes.delete(both)", ec2nodes.delete, "both", purge=False)
            _spin_until("waiting: EC2 instances gone", lambda: not _any_instances_in_vpc(), timeout=1200, poll=2.0)

        _run_step("routes.delete_private", routes.delete_private)  # idempotent

        # Big warning + explicit Y/N prompt, then a bold countdown with Enter/Cancel
        _confirm_nat_delete("Tearing down the 'routing' tier")
        _nat_delete_countdown(60, skippable=True)
        _run_step("natgw.delete", natgw.delete)
        _spin_until("waiting: NAT gateway gone", lambda: not _nat_exists(), timeout=900, poll=2.0)
    finally:
        _spinner_stop_now()

def down_network(purge: bool):
    _spinner_start()
    try:
        # Check once up front: will this run delete a NAT?
        need_nat_delete = _nat_exists()
        if need_nat_delete:
            _confirm_nat_delete("Deleting the VPC ('network' tier) will also delete NAT")

        # Cascade: ensure EC2 is gone first (so ENIs don't block NAT/subnets)
        if _any_instances_in_vpc():
            _run_step("ec2nodes.delete(both)", ec2nodes.delete, "both", purge=False)
            _spin_until("waiting: EC2 instances gone",
                        lambda: not _any_instances_in_vpc(), timeout=1200, poll=2.0)

        # Drop private route table (idempotent)
        _run_step("routes.delete_private", routes.delete_private)

        # If NAT still exists, proceed without re-prompting; just do the countdown
        if need_nat_delete and _nat_exists():
            _nat_delete_countdown(60, skippable=True)
            _run_step("natgw.delete", natgw.delete)
            _spin_until("waiting: NAT gateway gone",
                        lambda: not _nat_exists(), timeout=900, poll=2.0)

        # Finally, delete the VPC via dependency pipeline
        _run_step("vpc.delete", vpc.delete)
        _spin_until("waiting: VPC gone", lambda: not _vpc_exists(), timeout=600, poll=2.0)

        # Optional cleanups
        if purge:
            _run_step("ec2nodes._purge_keypair", ec2nodes._purge_keypair)
    finally:
        _spinner_stop_now()


# ------------------ CLI ------------------

def main():
    ap = argparse.ArgumentParser(description="Tiered full setup/teardown")
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="Bring up a tier (includes all lower tiers)")
    up.add_argument("--tier", choices=["network", "routing", "ec2"], required=True)

    down = sub.add_parser("down", help="Tear down a tier (cascades safely)")
    down.add_argument("--tier", choices=["ec2", "routing", "network"], required=True)
    down.add_argument("--purge", action="store_true",
                      help="Also remove SSH key pair (+ local PEM); for ec2 tier also removes SG if unused")

    sub.add_parser("status", help="Show status for all components")

    a = ap.parse_args()
    if a.cmd == "up":
        {"network": up_network, "routing": up_routing, "ec2": up_ec2}[a.tier]()
    elif a.cmd == "down":
        {"ec2": lambda: down_ec2(a.purge),
         "routing": down_routing,
         "network": lambda: down_network(a.purge)}[a.tier]()
    elif a.cmd == "status":
        status()

if __name__ == "__main__":
    main()
