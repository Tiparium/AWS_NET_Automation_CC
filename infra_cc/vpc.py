#!/usr/bin/env python3
"""
infra_cc/vpc.py
Create / Status / Delete for the assignment's VPC step.

- CIDR: 10.0.0.0/16
- Name tag: via infra_cc.naming (your scheme)
- Extra tag: SpecName=mar5-demo
- Delete: builds a dependency tree and deletes everything immediately (no prompt).
          full_setup handles the only confirmation (NAT) and all waiting/spinners.
"""

from __future__ import annotations
import argparse
from botocore.exceptions import ClientError

from .session import ec2
from .naming import res_name, tags_for
from .deps import Blocker, build_tree, prompt_and_delete, register_checker, register_deleter

# ----- Assignment specifics -----
VPC_CIDR   = "10.0.0.0/16"
SPEC_LABEL = "mar5-demo"
VPC_NAME   = res_name("vpc")  # e.g., nainoa-faulkner-jackson-vpc_HW3_CC

def _common_tags():
    return tags_for(VPC_NAME) + [{"Key": "SpecName", "Value": SPEC_LABEL}]

# ----- Find helpers -----
def find_vpc_id() -> str | None:
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

# ----- Dependency registration for "vpc", "internet-gateway", "security-group" -----
@register_checker("vpc")
def _check_vpc_blockers(vpc_id: str):
    """
    Immediate blockers for a VPC:
      - subnets
      - attached internet gateways
      - NON-DEFAULT security groups
    Subnet children (e.g., NAT, ENIs) are expanded by their own checkers.
    """
    c = ec2()
    blockers: list[Blocker] = []

    # Subnets in this VPC
    subs = c.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets", [])
    for s in subs:
        name = next((t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), None)
        blockers.append(Blocker(kind="subnet", id=s["SubnetId"], name=name))

    # Internet Gateways attached to this VPC
    igws = c.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    ).get("InternetGateways", [])
    for g in igws:
        name = next((t["Value"] for t in g.get("Tags", []) if t["Key"] == "Name"), None)
        blockers.append(Blocker(kind="internet-gateway", id=g["InternetGatewayId"], name=name))

    # Non-default Security Groups in this VPC (default is deleted with the VPC)
    sgs = c.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("SecurityGroups", [])
    for sg in sgs:
        if sg.get("GroupName") == "default":
            continue
        name = next((t["Value"] for t in sg.get("Tags", []) if t["Key"] == "Name"), None)
        blockers.append(Blocker(kind="security-group", id=sg["GroupId"], name=name))
    return blockers

@register_deleter("internet-gateway")
def _delete_igw(igw_id: str):
    """Detach IGW from any VPCs then delete."""
    c = ec2()
    try:
        igw = c.describe_internet_gateways(InternetGatewayIds=[igw_id])["InternetGateways"][0]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("InvalidInternetGatewayID.NotFound",):
            print(f"[ok] igw {igw_id} already gone")
            return
        raise

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
    try:
        c.delete_internet_gateway(InternetGatewayId=igw_id)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("InvalidInternetGatewayID.NotFound",):
            raise

@register_deleter("security-group")
def _delete_sg(sg_id: str):
    """Delete a non-default SG. Must not be referenced by ENIs."""
    c = ec2()
    print(f"[delete] security-group {sg_id}")
    try:
        c.delete_security_group(GroupId=sg_id)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("InvalidGroup.NotFound",):
            print(f"[ok] security-group {sg_id} already gone")
            return
        # If it's still attached to some ENI, bubble up so the pipeline errors loudly
        if code in ("DependencyViolation", "ResourceInUse"):
            raise SystemExit(f"[abort] security-group {sg_id} is still in use (likely attached to an ENI)")
        raise

@register_deleter("vpc")
def _delete_vpc(vpc_id: str):
    """Delete the VPC itself (assumes blockers have been removed)."""
    c = ec2()
    print(f"[delete] vpc {vpc_id}")
    try:
        c.delete_vpc(VpcId=vpc_id)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("InvalidVpcID.NotFound",):
            raise

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

    # sane defaults
    c.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    c.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    waiter = c.get_waiter("vpc_available")
    waiter.wait(VpcIds=[vpc_id])
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
    """Delete the VPC and all its discovered children immediately (no extra prompts)."""
    vpc_id = find_vpc_id()
    if not vpc_id:
        print(f"[ok] nothing to delete: {VPC_NAME} ({VPC_CIDR}) not found")
        return
    root = build_tree(kind="vpc", rid=vpc_id, name=VPC_NAME, reason="has dependent resources (if any)")
    prompt_and_delete(root, delete_root=True)  # no prompt inside deps.py
    print(f"[deleted-requested] {vpc_id}")

def main():
    ap = argparse.ArgumentParser(description="Create/Status/Delete the mar5-demo VPC (using your naming).")
    ap.add_argument("action", choices=["create", "status", "delete"])
    a = ap.parse_args()
    {"create": create, "status": status, "delete": delete}[a.action]()

if __name__ == "__main__":
    main()
