#!/usr/bin/env python3
"""
infra_cc/vpc.py
Create / Status / Delete for the assignment's first step:
- VPC CIDR: 10.0.0.0/16
- Name tag: uses your convention via infra_cc.naming
- Extra tag: SpecName=mar5-demo  (so you can map back to the professor's spec)

Idempotent + safe:
- We *find* the VPC by BOTH Name tag and CIDR (scoped to your account+region via infra_cc.session).
- If multiple matches somehow exist, we abort (refuse to touch ambiguous resources).
"""

from __future__ import annotations
import argparse, sys, time
from botocore.exceptions import ClientError

# Support running as a script OR as a package module
try:
    from .session import ec2
    from .naming import res_name, tags_for
except ImportError:
    from infra_cc.session import ec2
    from infra_cc.naming import res_name, tags_for

# ----- Assignment specifics -----
VPC_CIDR   = "10.0.0.0/16"
SPEC_LABEL = "mar5-demo"                    # professor's label
VPC_NAME   = res_name("vpc")                # e.g., nainoa-faulkner-jackson-vpc_HW3_CC

def _common_tags():
    """Base tags (Name/Stack/Owner/etc.) + SpecName tag for traceability."""
    return tags_for(VPC_NAME) + [{"Key": "SpecName", "Value": SPEC_LABEL}]

# ----- Find / Wait helpers -----
def find_vpc_id():
    """
    Return VpcId if exactly one VPC matches BOTH:
      - tag:Name == our VPC_NAME
      - cidr-block == VPC_CIDR
    Return None if not found. Abort if >1 matches (safety).
    """
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

# ----- Actions -----
def create():
    c = ec2()
    vpc_id = find_vpc_id()
    if vpc_id:
        print(f"[ok] VPC exists: {VPC_NAME} ({VPC_CIDR}) -> {vpc_id}")
        return

    resp = c.create_vpc(
        CidrBlock=VPC_CIDR,
        TagSpecifications=[{
            "ResourceType": "vpc",
            "Tags": _common_tags(),
        }],
    )
    vpc_id = resp["Vpc"]["VpcId"]
    print(f"[creating] {VPC_NAME} ({VPC_CIDR}) -> {vpc_id}")

    # sane defaults for later steps (DNS features)
    c.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    c.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    wait_available(c, vpc_id)
    print(f"[created] {vpc_id}")

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

    # Because this first step only creates the VPC (no subnets/IGW yet),
    # deletion should succeed immediately. Later, teardown must remove dependents first.
    print(f"[deleting] {VPC_NAME} -> {vpc_id}")
    c.delete_vpc(VpcId=vpc_id)
    if wait_deleted(c, vpc_id):
        print(f"[deleted] {vpc_id}")
    else:
        print(f"[warn] delete not yet confirmed for {vpc_id}")

# ----- CLI -----
def main():
    ap = argparse.ArgumentParser(description="Create/Status/Delete the mar5-demo VPC (using your naming).")
    ap.add_argument("action", choices=["create", "status", "delete"])
    a = ap.parse_args()
    {"create": create, "status": status, "delete": delete}[a.action]()

if __name__ == "__main__":
    main()
