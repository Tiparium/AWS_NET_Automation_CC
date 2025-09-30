#!/usr/bin/env python3
"""
infra_cc/natgw.py
Create / Status / Delete for a NAT Gateway with an Elastic IP.

- NAT lives in our public subnet: "outside" (mar5-demo-subnet-a) at 10.0.0.0/24
- Allocates and tags an Elastic IP if missing (reuses only if clearly free)
- Tags everything with your naming convention + Spec tags
- Registers a 'nat-gateway' deleter for the dependency pipeline
- Adds a CLI spinner + elapsed timer for create/delete waits

Run (from project root):
  python -m infra_cc.natgw create
  python -m infra_cc.natgw status
  python -m infra_cc.natgw delete
"""

from __future__ import annotations
import argparse, time, sys
from botocore.exceptions import ClientError

from .session import ec2
from .naming  import res_name, tags_for
from . import vpc as vpc_mod
from .deps    import register_checker, register_deleter, build_tree, prompt_and_delete

# ---- Names / spec labels ----
NATGW_NAME   = res_name("natgw")       # e.g., nainoa-faulkner-jackson-natgw_HW3_CC
NAT_EIP_NAME = res_name("eip-nat")     # e.g., nainoa-faulkner-jackson-eip-nat_HW3_CC
SPEC_NG      = "mar5-demo-ngw"
SPEC_EIP     = "mar5-demo-ngw-eip"

# Public subnet (where NAT must live) is our "outside" subnet
PUBLIC_SUBNET_NAME = res_name("subnet-outside")
PUBLIC_SUBNET_CIDR = "10.0.0.0/24"

def _tags_nat():
    return tags_for(NATGW_NAME) + [{"Key": "SpecName", "Value": SPEC_NG}]

def _tags_eip():
    return tags_for(NAT_EIP_NAME) + [{"Key": "SpecName", "Value": SPEC_EIP}]

# ---------- Pretty CLI helpers ----------
_SPINNER_FRAMES = "|/-\\"

def _fmt_elapsed(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"

def _wait_nat_state(nat_id: str, target: set[str], timeout: int = 900, poll: float = 5.0, phase: str = "") -> str:
    """
    Show a spinner + elapsed timer until NAT reaches any state in `target`.
    Handles NotFound for delete waits.
    """
    c = ec2()
    start = time.time()
    last_state = None
    last_check = 0.0
    i = 0
    # initial one-line so users see something immediately
    sys.stdout.write(f"[wait] NAT {nat_id} {phase} | state=... | elapsed 00:00 {_SPINNER_FRAMES[0]}")
    sys.stdout.flush()

    while True:
        now = time.time()
        elapsed = now - start

        # Re-check state on the poll cadence
        if elapsed - last_check >= poll:
            last_check = elapsed
            try:
                gw = c.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]
                last_state = gw.get("State")
                if last_state in target:
                    sys.stdout.write(f"\r[wait] NAT {nat_id} {phase} | state={last_state} | elapsed {_fmt_elapsed(elapsed)}   \n")
                    sys.stdout.flush()
                    return last_state
                if last_state == "failed":
                    sys.stdout.write(f"\r[wait] NAT {nat_id} {phase} | state=failed | elapsed {_fmt_elapsed(elapsed)}   \n")
                    sys.stdout.flush()
                    raise SystemExit(f"[abort] NAT {nat_id} entered state=failed")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("NatGatewayNotFound", "InvalidNatGatewayID.NotFound"):
                    if "deleted" in target:
                        sys.stdout.write(f"\r[wait] NAT {nat_id} {phase} | state=deleted | elapsed {_fmt_elapsed(elapsed)}   \n")
                        sys.stdout.flush()
                        return "deleted"
                else:
                    sys.stdout.write("\n")
                    raise

        # Timeout?
        if elapsed >= timeout:
            sys.stdout.write(f"\r[wait] NAT {nat_id} {phase} | state={last_state or '...'} | elapsed {_fmt_elapsed(elapsed)}   \n")
            sys.stdout.flush()
            raise SystemExit(f"[timeout] NAT {nat_id} did not reach {target} within {_fmt_elapsed(elapsed)}")

        # Animate spinner smoothly
        frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
        sys.stdout.write(f"\r[wait] NAT {nat_id} {phase} | state={last_state or '...'} | elapsed {_fmt_elapsed(elapsed)} {frame}")
        sys.stdout.flush()
        time.sleep(0.1)
        i += 1

# ---- Find helpers ----
def _find_public_subnet_id() -> str | None:
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        return None
    resp = c.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "cidr-block", "Values": [PUBLIC_SUBNET_CIDR]},
            {"Name": "tag:Name", "Values": [PUBLIC_SUBNET_NAME]},
        ]
    )
    subs = resp.get("Subnets", [])
    if not subs:
        return None
    if len(subs) > 1:
        raise SystemExit(f"[abort] multiple public subnets match {PUBLIC_SUBNET_NAME} {PUBLIC_SUBNET_CIDR}")
    return subs[0]["SubnetId"]

def _find_natgw() -> tuple[str | None, str | None, str | None, str | None]:
    """
    Return (nat_gateway_id, subnet_id, allocation_id, state) for an *active* NAT by Name tag.
    Ignore tombstones in 'deleting'/'deleted' so create() can proceed immediately.
    """
    c = ec2()
    resp = c.describe_nat_gateways(
        Filters=[
            {"Name": "tag:Name", "Values": [NATGW_NAME]},
            {"Name": "state",    "Values": ["pending", "available"]},  # key filter
        ]
    )
    ngws = resp.get("NatGateways", [])
    if not ngws:
        return (None, None, None, None)
    if len(ngws) > 1:
        raise SystemExit(f"[abort] multiple active NAT gateways named {NATGW_NAME}")
    g = ngws[0]
    allocs = g.get("NatGatewayAddresses", [])
    alloc_id = allocs[0].get("AllocationId") if allocs else None
    return (g["NatGatewayId"], g.get("SubnetId"), alloc_id, g.get("State"))

def _find_eip_allocation() -> str | None:
    """
    Find our tagged EIP allocation if it's not in use.
    If it's still associated (e.g., ghosting on a NAT/ENI), return None so we allocate a fresh one.
    """
    c = ec2()
    addrs = c.describe_addresses(
        Filters=[{"Name": "tag:Name", "Values": [NAT_EIP_NAME]}]
    ).get("Addresses", [])
    if not addrs:
        return None
    # Prefer an address that isn't associated to anything
    for a in addrs:
        if not a.get("AssociationId") and not a.get("NetworkInterfaceId"):
            return a["AllocationId"]
    # Otherwise, safer to allocate a fresh one
    return None

def _allocate_eip_tagged() -> str:
    c = ec2()
    resp = c.allocate_address(Domain="vpc")
    alloc_id = resp["AllocationId"]
    c.create_tags(Resources=[alloc_id], Tags=_tags_eip())
    print(f"[allocated] EIP -> {alloc_id}")
    return alloc_id

# ---- Actions ----
def create() -> None:
    total_start = time.time()

    # Ensure VPC + public subnet
    vpc_id = vpc_mod.create()
    subnet_id = _find_public_subnet_id()
    if not subnet_id:
        raise SystemExit(f"[abort] public subnet not found: {PUBLIC_SUBNET_NAME} ({PUBLIC_SUBNET_CIDR}). "
                         f"Run: python -m infra_cc.subnet create --which outside")

    nat_id, nat_subnet, alloc_id, state = _find_natgw()
    if nat_id:
        if nat_subnet != subnet_id:
            raise SystemExit(f"[abort] NAT {nat_id} exists in a different subnet {nat_subnet}; expected {subnet_id}")
        print(f"[ok] NAT exists: {nat_id} in subnet {subnet_id} (state={state}, eip_alloc={alloc_id})")
        return

    # Ensure we have a tagged EIP allocation
    alloc_id = _find_eip_allocation() or _allocate_eip_tagged()

    # Create the NAT gateway
    c = ec2()
    resp = c.create_nat_gateway(SubnetId=subnet_id, AllocationId=alloc_id)
    nat_id = resp["NatGateway"]["NatGatewayId"]
    c.create_tags(Resources=[nat_id], Tags=_tags_nat())
    print(f"[creating] NAT -> {nat_id} (subnet={subnet_id}, eip_alloc={alloc_id})")

    # Wait until available (spinner)
    final_state = _wait_nat_state(nat_id, {"available", "failed"}, phase="becoming available")
    if final_state != "available":
        raise SystemExit(f"[abort] NAT {nat_id} entered state={final_state}")

    print(f"[created] NAT {nat_id} (state=available) in {_fmt_elapsed(time.time() - total_start)}")

def status() -> None:
    nat_id, subnet_id, alloc_id, state = _find_natgw()
    if not nat_id:
        print("[status] NAT: NOT FOUND")
        return
    print(f"[status] NAT={nat_id} state={state} subnet={subnet_id} eip_alloc={alloc_id}")

@register_checker("nat-gateway")
def _check_nat_blockers(nat_id: str):
    # NAT has no child blockers in our model (leaf)
    return []

@register_deleter("nat-gateway")
def _delete_nat(nat_id: str) -> None:
    """Delete NAT and then release its Elastic IP allocation, with spinner."""
    c = ec2()
    alloc_id = None
    try:
        gw = c.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]
        addrs = gw.get("NatGatewayAddresses", [])
        alloc_id = addrs[0].get("AllocationId") if addrs else None
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NatGatewayNotFound", "InvalidNatGatewayID.NotFound"):
            print(f"[delete] nat {nat_id} already gone")
            alloc_id = None
        else:
            raise

    print(f"[delete] nat {nat_id}")
    try:
        c.delete_nat_gateway(NatGatewayId=nat_id)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NatGatewayNotFound", "InvalidNatGatewayID.NotFound"):
            raise

    # Wait until fully deleted before releasing EIP (spinner)
    try:
        _wait_nat_state(nat_id, {"deleted"}, phase="deleting")
    except SystemExit:
        # Treat timeout as best-effort; many accounts return NotFound quickly
        pass

    if alloc_id:
        try:
            print(f"[release] eip allocation {alloc_id}")
            c.release_address(AllocationId=alloc_id)
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("InvalidAllocationID.NotFound", "AuthFailure"):
                raise

def delete() -> None:
    nat_id, _, _, _ = _find_natgw()
    if not nat_id:
        print("[ok] nothing to delete: NAT not found")
        return

    # NAT is a leaf; the framework will skip prompt if there are no children
    root = build_tree(kind="nat-gateway", rid=nat_id, name=NATGW_NAME, reason="delete NAT then release EIP")
    try:
        prompt_and_delete(root, delete_root=True)
        print(f"[deleted] NAT {nat_id}")
    except Exception as e:
        print(f"[abort] {e}")

def main():
    ap = argparse.ArgumentParser(description="NAT Gateway create/status/delete")
    ap.add_argument("action", choices=["create", "status", "delete"])
    a = ap.parse_args()
    {"create": create, "status": status, "delete": delete}[a.action]()

if __name__ == "__main__":
    main()
