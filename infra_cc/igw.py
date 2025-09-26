#!/usr/bin/env python3
"""
infra_cc/igw.py
Internet Gateway for our mar5-demo VPC.

- Name tag: via infra_cc.naming (your scheme)
- Extra tag: SpecName=mar5-demo-igw
- create           : creates an IGW with our tags (unattached)
- attach           : attaches our IGW to our VPC (idempotent)
- create-attach    : convenience (create if missing, then attach)
- status           : prints IGW id + attachment
- delete           : uses the generic dependency pipeline to delete this IGW
                    (no children -> no prompt; safely detaches then deletes)

Run (from project root):
  python -m infra_cc.igw create-attach
  python -m infra_cc.igw status
  python -m infra_cc.igw delete
"""

from __future__ import annotations
import argparse
from botocore.exceptions import ClientError

from .session import ec2
from .naming import res_name, tags_for
from . import vpc as vpc_mod
from .deps import register_deleter, build_tree, prompt_and_delete

IGW_NAME    = res_name("igw")
SPEC_LABEL  = "mar5-demo-igw"

def _tags():
    return tags_for(IGW_NAME) + [{"Key": "SpecName", "Value": SPEC_LABEL}]

def find_igw() -> tuple[str | None, str | None]:
    """
    Return (igw_id, attached_vpc_id). If not found, (None, None).
    We search by Name tag == IGW_NAME.
    """
    c = ec2()
    resp = c.describe_internet_gateways(
        Filters=[{"Name": "tag:Name", "Values": [IGW_NAME]}]
    )
    igws = resp.get("InternetGateways", [])
    if not igws:
        return (None, None)
    if len(igws) > 1:
        raise SystemExit(f"[abort] multiple IGWs tagged Name={IGW_NAME}")
    igw = igws[0]
    atts = igw.get("Attachments", [])
    vpc_id = atts[0]["VpcId"] if atts else None
    return (igw["InternetGatewayId"], vpc_id)

def create() -> str:
    c = ec2()
    igw_id, attached = find_igw()
    if igw_id:
        print(f"[ok] IGW exists: {IGW_NAME} -> {igw_id} (attached_to={attached})")
        return igw_id
    resp = c.create_internet_gateway(
        TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": _tags()}]
    )
    igw_id = resp["InternetGateway"]["InternetGatewayId"]
    print(f"[created] IGW -> {igw_id}")
    return igw_id

def attach() -> None:
    c = ec2()
    vpc_id = vpc_mod.create()  # ensure our VPC exists (returns vpc id)
    igw_id, attached = find_igw()
    if not igw_id:
        igw_id = create()
        attached = None

    if attached == vpc_id:
        print(f"[ok] IGW already attached: {igw_id} -> {vpc_id}")
        return
    if attached and attached != vpc_id:
        raise SystemExit(f"[abort] IGW {igw_id} is attached to a different VPC {attached}")

    c.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    print(f"[attached] {igw_id} -> {vpc_id}")

def create_attach() -> None:
    create()
    attach()

def status() -> None:
    igw_id, attached = find_igw()
    if not igw_id:
        print(f"[status] IGW {IGW_NAME}: NOT FOUND")
        return
    print(f"[status] IGW={igw_id} attached_to={attached}")

@register_deleter("internet-gateway")
def _delete_igw(igw_id: str) -> None:
    """Deleter for the pipeline: safely detach from any VPCs, then delete."""
    c = ec2()
    try:
        igw = c.describe_internet_gateways(InternetGatewayIds=[igw_id])["InternetGateways"][0]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("InvalidInternetGatewayID.NotFound",):
            print(f"[delete] igw {igw_id} already gone")
            return
        raise

    # Detach from all attachments (normally one)
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

def delete() -> None:
    """
    Deletion entrypoint that leverages the pipeline:
      - Build tree at the IGW itself (no children -> no prompt).
      - Pipeline calls our registered deleter above.
    """
    igw_id, _ = find_igw()
    if not igw_id:
        print(f"[ok] nothing to delete: IGW {IGW_NAME} not found")
        return

    root = build_tree(kind="internet-gateway", rid=igw_id, name=IGW_NAME, reason="detach then delete")
    try:
        # No dependencies -> framework will skip prompt and just delete.
        prompt_and_delete(root, delete_root=True)
        print(f"[deleted] igw {igw_id}")
    except Exception as e:
        print(f"[abort] {e}")

def main():
    ap = argparse.ArgumentParser(description="Internet Gateway create/attach/status/delete")
    ap.add_argument("action", choices=["create", "attach", "create-attach", "status", "delete"])
    a = ap.parse_args()
    {"create": create, "attach": attach, "create-attach": create_attach, "status": status, "delete": delete}[a.action]()

if __name__ == "__main__":
    main()
