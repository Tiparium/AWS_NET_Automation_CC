#!/usr/bin/env python3
"""
infra_cc/vpc.py
Create / Status / Delete for the assignment's VPC step.

- CIDR: 10.0.0.0/16
- Name tag: via infra_cc.naming (your scheme)
- Extra tag: SpecName=mar5-demo
- Delete: builds a dependency tree generically (via deps.py). If blockers exist,
          it prints the tree and prompts [y/N] to delete blockers in order,
          then deletes the VPC.
"""

from __future__ import annotations
import argparse, time
from botocore.exceptions import ClientError

# Package imports (run with: python -m infra_cc.vpc ...)
from .session import ec2
from .naming import res_name, tags_for
from .deps import Blocker, build_tree, prompt_and_delete, register_checker, register_deleter

# ----- Assignment specifics -----
VPC_CIDR   = "10.0.0.0/16"
SPEC_LABEL = "mar5-demo"
VPC_NAME   = res_name("vpc")  # e.g., nainoa-faulkner-jackson-vpc_HW3_CC

def _common_tags():
    return tags_for(VPC_NAME) + [{"Key": "SpecName", "Value": SPEC_LABEL}]

# ----- Find / Wait helpers -----
def find_vpc_id() -> str | None:
    """Return VpcId for the single VPC matching Name+CIDR; else None."""
    c = ec2()
    resp = c.describe_vpcs(
        Filters=[
            {"Name": "tag:Name",   "Values": [VPC_NAME]},
            {"Name": "cidr-block", "Values": [VPC_CIDR]},
        ]
    )
    vpcs = resp.get("Vpcs", [])
    if not vpcs:
        return None
    if len(vpcs) > 1:
        raise SystemExit(f"[abort] multiple VPCs match Name={VPC_NAME} CIDR={VPC_CIDR}; resolve manually.")
    return vpcs[0]["VpcId"]

def wait_available(c, vpc_id: str, timeout=60):
    waiter = c.get_waiter("vpc_available")
    waiter.wait(VpcIds=[vpc_id], WaiterConfig={"Delay": 2, "MaxAttempts": max(1, timeout // 2)})

def wait_deleted(c, vpc_id: str, timeout=60) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = c.describe_vpcs(VpcIds=[vpc_id])
            if not resp.get("Vpcs"):
                return True
            time.sleep(2)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("InvalidVpcID.NotFound", "InvalidVpcID.Malformed"):
                return True
            time.sleep(2)
    return False

# ----- Dependency registration for "vpc" and "internet-gateway" -----
@register_checker("vpc")
def _check_vpc_blockers(vpc_id: str):
    """Immediate blockers for a VPC: subnets and attached internet gateways."""
    c = ec2()
    blockers: list[Blocker] = []

    # Subnets in this VPC (children will be expanded via their own checker)
    subs = c.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}]).get("Subnets", [])
    for s in subs:
        name = next((t["Value"] for t in s.get("Tags", []) if t["Key"]=="Name"), None)
        blockers.append(Blocker(kind="subnet", id=s["SubnetId"], name=name))

    # Internet Gateways attached to this VPC
    igws = c.describe_internet_gateways(
        Filters=[{"Name":"attachment.vpc-id","Values":[vpc_id]}]
    ).get("InternetGateways", [])
    for g in igws:
        name = next((t["Value"] for t in g.get("Tags", []) if t["Key"]=="Name"), None)
        blockers.append(Blocker(kind="internet-gateway", id=g["InternetGatewayId"], name=name))

    return blockers

@register_deleter("internet-gateway")
def _delete_igw(igw_id: str):
    """Detach IGW from any VPCs then delete. (Temporary home until igw.py exists.)"""
    c = ec2()
    # Detach from all attachments (normally one)
    igw = c.describe_internet_gateways(InternetGatewayIds=[igw_id])["InternetGateways"][0]
    for att in igw.get("Attachments", []):
        vpc_id = att.get("VpcId")
        if vpc_id:
            try:
                print(f"[detach] igw {igw_id} from vpc {vpc_id}")
                c.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("Gateway.NotAttached", "InvalidInternetGatewayID.NotFound"):
                    raise
    print(f"[delete] igw {igw_id}")
    c.delete_internet_gateway(InternetGatewayId=igw_id)

@register_deleter("vpc")
def _delete_vpc(vpc_id: str):
    """Delete the VPC itself (assumes blockers have been removed)."""
    c = ec2()
    print(f"[delete] vpc {vpc_id}")
    c.delete_vpc(VpcId=vpc_id)

# ----- Actions -----
def create() -> str:
    c = ec2()
    vpc_id = find_vpc_id()
    if vpc_id:
        print(f"[ok] VPC exists: {VPC_NAME} ({VPC_CIDR}) -> {vpc_id}")
        return vpc_id

    resp = c.create_vpc(
        CidrBlock=VPC_CIDR,
        TagSpecifications=[{
            "ResourceType": "vpc",
            "Tags": _common_tags(),
        }],
    )
    vpc_id = resp["Vpc"]["VpcId"]
    print(f"[creating] {VPC_NAME} ({VPC_CIDR}) -> {vpc_id}")

    # sane defaults for later steps
    c.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    c.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    wait_available(c, vpc_id)
    print(f"[created] {vpc_id}")
    return vpc_id

def status():
    c = ec2()
    vpc_id = find_vpc_id()
    if not vpc_id:
        print(f"[status] {VPC_NAME} ({VPC_CIDR}): NOT FOUND")
        return
    v = c.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
    print(f"[status] FOUND -> {vpc_id}")
    print(f"  State={v['State']}  IsDefault={v['IsDefault']}  Tenancy={v['InstanceTenancy']}")
    print(f"  CIDR={v['CidrBlock']}  Tags={tags}")

def delete():
    c = ec2()
    vpc_id = find_vpc_id()
    if not vpc_id:
        print(f"[ok] nothing to delete: {VPC_NAME} ({VPC_CIDR}) not found")
        return

    root = build_tree(kind="vpc", rid=vpc_id, name=VPC_NAME, reason="has dependent resources (if any)")
    try:
        # If there are children, prompt; if none, this still deletes the VPC.
        prompt_and_delete(root, delete_root=True)
    except Exception as e:
        # If user declined or missing deleters, just report and exit gracefully.
        print(f"[abort] {e}")
        return

    # Wait for final VPC disappearance
    if wait_deleted(c, vpc_id):
        print(f"[deleted] {vpc_id}")
    else:
        print(f"[warn] delete not yet confirmed for {vpc_id}")

def main():
    ap = argparse.ArgumentParser(description="Create/Status/Delete the mar5-demo VPC (using your naming).")
    ap.add_argument("action", choices=["create", "status", "delete"])
    a = ap.parse_args()
    {"create": create, "status": status, "delete": delete}[a.action]()

if __name__ == "__main__":
    main()
