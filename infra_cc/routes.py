#!/usr/bin/env python3
"""
infra_cc/routes.py
Private route table for the private subnet + public main default route.

- Private RT: nainoa-faulkner-jackson-rt-private_HW3_CC
  * Associate to VPC
  * Associate to private subnet ("inside" / 10.0.1.0/24)
  * Default route 0.0.0.0/0 -> NAT
- Public (main) RT:
  * Default route 0.0.0.0/0 -> IGW

Idempotent operations. Includes status and delete of the private RT.
"""

from __future__ import annotations
import argparse
from botocore.exceptions import ClientError

from .session import ec2
from .naming  import res_name, tags_for
from . import vpc as vpc_mod
from .igw import find_igw as _find_igw
from .natgw import NATGW_NAME  # for consistent Name usage

RT_PRIVATE_NAME = res_name("rt-private")
SPEC_PRIVATE    = "mar5-demo-rt-private"

PRIVATE_SUBNET_NAME = res_name("subnet-inside")
PRIVATE_SUBNET_CIDR = "10.0.1.0/24"

def _tags_private():
    return tags_for(RT_PRIVATE_NAME) + [{"Key": "SpecName", "Value": SPEC_PRIVATE}]

def _find_private_subnet_id() -> str | None:
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        return None
    resp = c.describe_subnets(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"cidr-block","Values":[PRIVATE_SUBNET_CIDR]},
        {"Name":"tag:Name","Values":[PRIVATE_SUBNET_NAME]},
    ])
    subs = resp.get("Subnets", [])
    if not subs: return None
    if len(subs) > 1: raise SystemExit(f"[abort] multiple private subnets match {PRIVATE_SUBNET_NAME} {PRIVATE_SUBNET_CIDR}")
    return subs[0]["SubnetId"]

def _find_private_rt_id() -> str | None:
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id: return None
    r = c.describe_route_tables(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"tag:Name","Values":[RT_PRIVATE_NAME]},
    ]).get("RouteTables", [])
    if not r: return None
    if len(r) > 1: raise SystemExit(f"[abort] multiple route tables named {RT_PRIVATE_NAME}")
    return r[0]["RouteTableId"]

def _find_main_rt_id() -> str:
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id: raise SystemExit("[abort] VPC not found for main route table")
    r = c.describe_route_tables(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"association.main","Values":["true"]},
    ]).get("RouteTables", [])
    if not r: raise SystemExit("[abort] main route table not found")
    return r[0]["RouteTableId"]

def create_private() -> str:
    c = ec2()
    vpc_id = vpc_mod.create()
    rt_id = _find_private_rt_id()
    if rt_id:
        print(f"[ok] private RT exists: {RT_PRIVATE_NAME} -> {rt_id}")
        return rt_id
    rt = c.create_route_table(VpcId=vpc_id)["RouteTable"]
    rt_id = rt["RouteTableId"]
    c.create_tags(Resources=[rt_id], Tags=_tags_private())
    print(f"[created] private RT -> {rt_id}")
    return rt_id

def _ensure_subnet_association(rt_id: str, subnet_id: str):
    c = ec2()
    # Is this subnet already associated?
    r = c.describe_route_tables(Filters=[
        {"Name":"association.subnet-id","Values":[subnet_id]}
    ]).get("RouteTables", [])
    if r:
        assoc = r[0]["Associations"][0]
        cur_rt = r[0]["RouteTableId"]
        if cur_rt == rt_id:
            print(f"[ok] subnet {subnet_id} already associated with {rt_id}")
            return
        # Replace association to our RT
        print(f"[assoc] replacing association {assoc['RouteTableAssociationId']} -> {rt_id}")
        c.replace_route_table_association(AssociationId=assoc["RouteTableAssociationId"], RouteTableId=rt_id)
        return
    # No association yet
    c.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)
    print(f"[assoc] subnet {subnet_id} -> {rt_id}")

def set_private_default():
    c = ec2()
    rt_id = _find_private_rt_id() or create_private()
    subnet_id = _find_private_subnet_id()
    if not subnet_id:
        raise SystemExit(f"[abort] private subnet not found: {PRIVATE_SUBNET_NAME} ({PRIVATE_SUBNET_CIDR})")
    _ensure_subnet_association(rt_id, subnet_id)

    # Find an AVAILABLE NAT with our Name (ignore tombstones)
    resp = c.describe_nat_gateways(Filters=[
        {"Name":"tag:Name","Values":[NATGW_NAME]},
        {"Name":"state","Values":["available"]},
    ])
    ngws = resp.get("NatGateways", [])
    if not ngws:
        raise SystemExit("[abort] NAT gateway not found/available. Run: python -m infra_cc.natgw create")
    nat_id = ngws[0]["NatGatewayId"]

    # Upsert: try create, fall back to replace if it already exists
    try:
        c.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)
        print(f"[route] {rt_id}: 0.0.0.0/0 -> {nat_id} (created)")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("RouteAlreadyExists", "InvalidRoute.Duplicate"):
            c.replace_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)
            print(f"[route] {rt_id}: 0.0.0.0/0 -> {nat_id} (replaced)")
        else:
            raise


def set_public_main():
    c = ec2()
    rt_main = _find_main_rt_id()
    igw_id, attached = _find_igw()
    if not igw_id or not attached:
        raise SystemExit("[abort] IGW not found/attached. Run: python -m infra_cc.igw create-attach")

    # Upsert: try create, fall back to replace if it already exists
    try:
        c.create_route(RouteTableId=rt_main, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        print(f"[route] main {rt_main}: 0.0.0.0/0 -> {igw_id} (created)")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("RouteAlreadyExists", "InvalidRoute.Duplicate"):
            c.replace_route(RouteTableId=rt_main, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
            print(f"[route] main {rt_main}: 0.0.0.0/0 -> {igw_id} (replaced)")
        else:
            raise


def status():
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        print("[status] VPC: NOT FOUND")
        return

    # Private RT
    rt_id = _find_private_rt_id()
    if not rt_id:
        print(f"[status] private RT {RT_PRIVATE_NAME}: NOT FOUND")
    else:
        rt = c.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
        assoc_subnets = [a.get("SubnetId") for a in rt.get("Associations", []) if not a.get("Main")]
        default = next((r for r in rt.get("Routes", []) if r.get("DestinationCidrBlock")=="0.0.0.0/0"), {})
        target = default.get("NatGatewayId") or default.get("GatewayId") or default.get("NetworkInterfaceId")
        print(f"[status] private RT {rt_id} assoc={assoc_subnets} default-> {target}")

    # Main RT
    rt_main = _find_main_rt_id()
    rt = c.describe_route_tables(RouteTableIds=[rt_main])["RouteTables"][0]
    default = next((r for r in rt.get("Routes", []) if r.get("DestinationCidrBlock")=="0.0.0.0/0"), {})
    target = default.get("GatewayId") or default.get("NatGatewayId") or default.get("NetworkInterfaceId")
    print(f"[status] main RT {rt_main} default-> {target}")

def delete_private():
    c = ec2()
    rt_id = _find_private_rt_id()
    if not rt_id:
        print(f"[ok] nothing to delete: {RT_PRIVATE_NAME}")
        return
    rt = c.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
    for a in rt.get("Associations", []):
        if a.get("Main"):
            continue
        print(f"[disassoc] {a['RouteTableAssociationId']}")
        c.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
    print(f"[delete] route-table {rt_id}")
    c.delete_route_table(RouteTableId=rt_id)

def main():
    ap = argparse.ArgumentParser(description="Routes create/status/delete")
    ap.add_argument("action", choices=["create-private", "set-private-default", "set-public-main", "status", "delete-private"])
    a = ap.parse_args()
    {"create-private": create_private, "set-private-default": set_private_default,
     "set-public-main": set_public_main, "status": status, "delete-private": delete_private}[a.action]()

if __name__ == "__main__":
    main()
