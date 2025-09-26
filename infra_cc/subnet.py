#!/usr/bin/env python3
"""
infra_cc/subnet.py
Create / Status / Delete of the two assignment subnets in the class VPC.

Mapping per spec:
  - inside  -> mar5-demo-subnet-a -> 10.0.0.0/24  (auto-assign public IP: YES)
  - outside -> mar5-demo-subnet-b -> 10.0.1.0/24  (auto-assign public IP: NO)

Run (from repo root):
  python -m infra_cc.subnet create --which both
  python -m infra_cc.subnet status --which inside
  python -m infra_cc.subnet delete --which outside
"""

from __future__ import annotations
import argparse
from botocore.exceptions import ClientError

from .session import ec2
from .naming import res_name, tags_for
from . import vpc as vpc_mod
from .deps import register_checker, register_deleter, Blocker, build_tree, prompt_and_delete

# Configuration for both subnets
SUBNETS = {
    "inside": {   # private
        "cidr": "10.0.1.0/24",
        "name": res_name("subnet-inside"),
        "auto_public": False,
        "spec_label": "mar5-demo-subnet-b",
    },
    "outside": {  # public
        "cidr": "10.0.0.0/24",
        "name": res_name("subnet-outside"),
        "auto_public": True,
        "spec_label": "mar5-demo-subnet-a",
    },
}

ORDER = ("inside", "outside")  # deterministic order for "both"

def _tags(which: str):
    cfg = SUBNETS[which]
    return tags_for(cfg["name"]) + [{"Key": "SpecName", "Value": cfg["spec_label"]}]

def _find(which: str) -> str | None:
    """Return SubnetId if exactly one subnet matches Name+CIDR in our VPC; else None."""
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        return None
    cfg = SUBNETS[which]
    resp = c.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "cidr-block", "Values": [cfg["cidr"]]},
            {"Name": "tag:Name", "Values": [cfg["name"]]},
        ]
    )
    subs = resp.get("Subnets", [])
    if not subs:
        return None
    if len(subs) > 1:
        raise SystemExit(f"[abort] multiple {which} subnets match Name+CIDR in VPC {vpc_id}")
    return subs[0]["SubnetId"]

def create(which: str) -> None:
    c = ec2()
    # Ensure VPC exists and get its ID
    vpc_id = vpc_mod.create()

    if which == "both":
        for w in ORDER:
            create(w)
        return

    cfg = SUBNETS[which]
    existing = _find(which)
    if existing:
        print(f"[ok] {which} subnet exists: {cfg['name']} ({cfg['cidr']}) -> {existing}")
        return

    # Pick the first AZ in the region for simplicity
    az = c.describe_availability_zones(
        Filters=[{"Name": "region-name", "Values": [c.meta.region_name]}]
    )["AvailabilityZones"][0]["ZoneName"]

    resp = c.create_subnet(
        VpcId=vpc_id,
        CidrBlock=cfg["cidr"],
        AvailabilityZone=az,
        TagSpecifications=[{"ResourceType": "subnet", "Tags": _tags(which)}],
    )
    sub_id = resp["Subnet"]["SubnetId"]
    # Configure auto-assign public IP setting
    c.modify_subnet_attribute(SubnetId=sub_id, MapPublicIpOnLaunch={"Value": bool(cfg["auto_public"])})

    print(f"[created] {which} subnet -> {sub_id}")
    print(f"  Name={cfg['name']}  CIDR={cfg['cidr']}  AZ={az}  AutoPublic={cfg['auto_public']}")

def status(which: str) -> None:
    if which == "both":
        for w in ORDER:
            status(w)
        return

    c = ec2()
    sub_id = _find(which)
    cfg = SUBNETS[which]
    if not sub_id:
        print(f"[status] {which} subnet ({cfg['name']} {cfg['cidr']}): NOT FOUND")
        return
    s = c.describe_subnets(SubnetIds=[sub_id])["Subnets"][0]
    tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
    print(f"[status] {which} subnet -> {sub_id}")
    print(f"  CIDR={s['CidrBlock']}  AZ={s['AvailabilityZone']}  MapPublicIpOnLaunch={s.get('MapPublicIpOnLaunch')}")
    print(f"  Name={tags.get('Name')}  Tags={tags}")

def delete(which: str) -> None:
    if which == "both":
        # Delete outside first (future-proof for NAT), then inside
        for w in reversed(ORDER):
            delete(w)
        return

    c = ec2()
    sub_id = _find(which)
    cfg = SUBNETS[which]
    if not sub_id:
        print(f"[ok] nothing to delete: {which} subnet ({cfg['name']})")
        return

    # Build tree at the subnet as root and prompt; this works for ANY entrypoint
    root = build_tree(kind="subnet", rid=sub_id, name=cfg["name"], reason="has dependent resources (if any)")
    try:
        prompt_and_delete(root, delete_root=True)
    except Exception as e:
        print(f"[abort] {e}")
        return
    print(f"[deleted] {which} subnet -> {sub_id}")

# ---- Dependency + deleter registration for subnets ----
@register_checker("subnet")
def _check_subnet_blockers(subnet_id: str):
    """Report blockers that would prevent deleting a subnet (read-only)."""
    c = ec2()
    blockers: list[Blocker] = []

    # NAT gateways in this subnet (would block subnet deletion if present)
    try:
        ngws = c.describe_nat_gateways(
            Filters=[{"Name": "subnet-id", "Values": [subnet_id]}]
        ).get("NatGateways", [])
        for g in ngws:
            blockers.append(Blocker(
                kind="nat-gateway",
                id=g["NatGatewayId"],
                reason=f"state={g.get('State','unknown')}",
            ))
    except Exception:
        pass  # permissions vary; fail-soft

    # ENIs in this subnet (any attached interface blocks deletion)
    try:
        enis = c.describe_network_interfaces(
            Filters=[{"Name": "subnet-id", "Values": [subnet_id]}]
        ).get("NetworkInterfaces", [])
        for eni in enis:
            att = eni.get("Attachment") or {}
            owner = att.get("InstanceId") or att.get("NetworkInterfaceId") or "attached"
            blockers.append(Blocker(
                kind="eni",
                id=eni["NetworkInterfaceId"],
                reason=f"attached={owner}",
            ))
    except Exception:
        pass

    return blockers

@register_deleter("subnet")
def _delete_subnet(subnet_id: str):
    """Deleter for subnets (assumes blockers removed)."""
    c = ec2()
    print(f"[delete] subnet {subnet_id}")
    c.delete_subnet(SubnetId=subnet_id)

def main():
    ap = argparse.ArgumentParser(description="Create/Status/Delete inside/outside subnets.")
    ap.add_argument("action", choices=["create", "status", "delete"])
    ap.add_argument("--which", choices=["inside", "outside", "both"], required=True)
    a = ap.parse_args()
    {"create": create, "status": status, "delete": delete}[a.action](a.which)

if __name__ == "__main__":
    main()
