#!/usr/bin/env python3
"""
infra_cc/ec2nodes.py
Create / Status / Delete of two EC2 instances:
  - ec2-public   (public subnet, public IP)
  - ec2-private  (private subnet, no public IP)

Also ensures:
  - SSH security group (in VPC) allowing TCP/22 from anywhere (simple for demo)
  - Key pair for SSH

Supports --purge on delete to remove the SG (if unused) and the key pair + local PEM.
"""

from __future__ import annotations
import argparse, os, stat
from botocore.exceptions import ClientError

from .session import ec2
from .naming  import res_name, tags_for
from . import vpc as vpc_mod

PUBLIC_SUBNET_NAME  = res_name("subnet-outside")
PUBLIC_SUBNET_CIDR  = "10.0.0.0/24"
PRIVATE_SUBNET_NAME = res_name("subnet-inside")
PRIVATE_SUBNET_CIDR = "10.0.1.0/24"

NAME_PUBLIC  = res_name("ec2-public")
NAME_PRIVATE = res_name("ec2-private")
SG_NAME      = res_name("sg-ssh")
KEY_NAME     = res_name("key-ssh")
SPEC_SG      = "mar5-demo-ssh-sg"
SPEC_EC2PUB  = "mar5-demo-ec2-public"
SPEC_EC2PRV  = "mar5-demo-ec2-private"

INSTANCE_TYPE = "t3.micro"
KEY_PATH = f"./.keys/{KEY_NAME}.pem"

def _find_subnet_id(name: str, cidr: str) -> str:
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    r = c.describe_subnets(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"cidr-block","Values":[cidr]},
        {"Name":"tag:Name","Values":[name]},
    ]).get("Subnets", [])
    if not r: raise SystemExit(f"[abort] subnet not found: {name} ({cidr})")
    if len(r)>1: raise SystemExit(f"[abort] multiple subnets match {name} {cidr}")
    return r[0]["SubnetId"]

def _ensure_keypair() -> str:
    c = ec2()
    try:
        kp = c.describe_key_pairs(KeyNames=[KEY_NAME]).get("KeyPairs", [None])[0]
        if kp: return KEY_NAME
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
            raise
    kp = c.create_key_pair(KeyName=KEY_NAME, TagSpecifications=[{
        "ResourceType":"key-pair", "Tags": tags_for(KEY_NAME)
    }])
    os.makedirs("./.keys", exist_ok=True)
    with open(KEY_PATH, "w") as f:
        f.write(kp["KeyMaterial"])
    os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)
    print(f"[keypair] created {KEY_NAME}, saved private key to {KEY_PATH}")
    return KEY_NAME

def _ensure_sg() -> str:
    c = ec2()
    vpc_id = vpc_mod.find_vpc_id()
    r = c.describe_security_groups(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"group-name","Values":[SG_NAME]}
    ]).get("SecurityGroups", [])
    if r:
        sg_id = r[0]["GroupId"]
    else:
        resp = c.create_security_group(GroupName=SG_NAME, Description="SSH SG", VpcId=vpc_id,
                                       TagSpecifications=[{"ResourceType":"security-group","Tags": tags_for(SG_NAME)+[{"Key":"SpecName","Value":SPEC_SG}]}])
        sg_id = resp["GroupId"]
        print(f"[sg] created {SG_NAME} -> {sg_id}")

    # ingress ssh
    try:
        ec2().authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol":"tcp","FromPort":22,"ToPort":22,
                "IpRanges":[{"CidrIp":"0.0.0.0/0","Description":"ssh"}],
            }]
        )
        print(f"[sg] ingress ssh open on {sg_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise

    # egress all
    try:
        ec2().authorize_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0","Description":"all-egress"}]}]
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise

    return sg_id

def _latest_al2023_ami() -> str:
    imgs = ec2().describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name":"name","Values":["al2023-ami-*-x86_64"]},
            {"Name":"architecture","Values":["x86_64"]},
            {"Name":"state","Values":["available"]},
            {"Name":"virtualization-type","Values":["hvm"]},
            {"Name":"root-device-type","Values":["ebs"]},
        ]
    )["Images"]
    if not imgs:
        raise SystemExit("[abort] Could not find Amazon Linux 2023 AMI in this region")
    return sorted(imgs, key=lambda i: i["CreationDate"], reverse=True)[0]["ImageId"]

def _ensure_instance(name: str, spec_label: str, subnet_id: str, public_ip: bool, sg_id: str, key_name: str):
    c = ec2()
    r = c.describe_instances(
        Filters=[
            {"Name":"tag:Name","Values":[name]},
            {"Name":"instance-state-name","Values":["pending","running","stopping","stopped"]},
        ]
    )["Reservations"]
    if r:
        inst = r[0]["Instances"][0]
        iid = inst["InstanceId"]
        state = inst["State"]["Name"]
        if state == "stopped":
            print(f"[start] {name} -> {iid}")
            c.start_instances(InstanceIds=[iid])
            c.get_waiter("instance_running").wait(InstanceIds=[iid])
        print(f"[ok] instance {name} -> {iid} ({state})")
        return

    ami = _latest_al2023_ami()
    ni = {
        "SubnetId": subnet_id,
        "DeviceIndex": 0,
        "Groups": [sg_id],
        "AssociatePublicIpAddress": bool(public_ip),
    }
    resp = c.run_instances(
        ImageId=ami, InstanceType=INSTANCE_TYPE, KeyName=key_name,
        NetworkInterfaces=[ni],
        MinCount=1, MaxCount=1,
        TagSpecifications=[{
            "ResourceType":"instance",
            "Tags": tags_for(name)+[{"Key":"SpecName","Value":spec_label}],
        }]
    )
    iid = resp["Instances"][0]["InstanceId"]
    print(f"[launch] {name} -> {iid}")
    c.get_waiter("instance_running").wait(InstanceIds=[iid])
    print(f"[running] {name} -> {iid}")

def _find_instance_id(name: str) -> str | None:
    r = ec2().describe_instances(Filters=[
        {"Name":"tag:Name","Values":[name]},
        {"Name":"instance-state-name","Values":["pending","running","stopping","stopped"]},
    ])["Reservations"]
    return r[0]["Instances"][0]["InstanceId"] if r else None

def _get_sg_id() -> str | None:
    vpc_id = vpc_mod.find_vpc_id()
    if not vpc_id:
        return None
    r = ec2().describe_security_groups(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"group-name","Values":[SG_NAME]}
    ]).get("SecurityGroups", [])
    return r[0]["GroupId"] if r else None

def _sg_in_use(sg_id: str) -> bool:
    r = ec2().describe_network_interfaces(Filters=[{"Name":"group-id","Values":[sg_id]}]).get("NetworkInterfaces", [])
    return bool(r)

def _purge_keypair():
    c = ec2()
    try:
        c.delete_key_pair(KeyName=KEY_NAME)
        print(f"[purge] key-pair {KEY_NAME} deleted")
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
            raise
    if os.path.exists(KEY_PATH):
        try:
            os.remove(KEY_PATH)
            print(f"[purge] removed local key {KEY_PATH}")
        except OSError:
            pass

def create(which: str):
    vpc_mod.create()
    sg_id = _ensure_sg()
    key = _ensure_keypair()
    pub_subnet = _find_subnet_id(PUBLIC_SUBNET_NAME, PUBLIC_SUBNET_CIDR)
    prv_subnet = _find_subnet_id(PRIVATE_SUBNET_NAME, PRIVATE_SUBNET_CIDR)
    if which in ("public", "both"):
        _ensure_instance(NAME_PUBLIC,  "mar5-demo-ec2-public",  pub_subnet, True,  sg_id, key)
    if which in ("private", "both"):
        _ensure_instance(NAME_PRIVATE, "mar5-demo-ec2-private", prv_subnet, False, sg_id, key)

def status(which: str):
    for label, name in (("public", NAME_PUBLIC), ("private", NAME_PRIVATE)):
        if which != "both" and which != label:
            continue
        iid = _find_instance_id(name)
        print(f"[status] {name}: {iid or 'NOT FOUND'}")
    sg_id = _get_sg_id()
    print(f"[status] SG {SG_NAME}: {sg_id or 'NOT FOUND'}")
    # keypair existence
    try:
        ec2().describe_key_pairs(KeyNames=[KEY_NAME])
        print(f"[status] KeyPair {KEY_NAME}: FOUND (local: {'yes' if os.path.exists(KEY_PATH) else 'no'})")
    except ClientError:
        print(f"[status] KeyPair {KEY_NAME}: NOT FOUND (local: {'yes' if os.path.exists(KEY_PATH) else 'no'})")

def delete(which: str, purge: bool = False):
    c = ec2()
    ids = []
    if which in ("public","both"):
        iid = _find_instance_id(NAME_PUBLIC)
        if iid: ids.append(iid)
    if which in ("private","both"):
        iid = _find_instance_id(NAME_PRIVATE)
        if iid: ids.append(iid)

    if ids:
        print(f"[terminate] {' '.join(ids)}")
        c.terminate_instances(InstanceIds=ids)
        c.get_waiter("instance_terminated").wait(InstanceIds=ids)
        print("[terminated]")
    else:
        print("[ok] no instances to terminate")

    if purge:
        # Delete SG (if VPC still exists and SG is unused)
        sg_id = _get_sg_id()
        if sg_id:
            if _sg_in_use(sg_id):
                print(f"[warn] SG {sg_id} still in use; not deleting")
            else:
                try:
                    ec2().delete_security_group(GroupId=sg_id)
                    print(f"[purge] deleted SG {sg_id}")
                except ClientError as e:
                    if e.response["Error"]["Code"] != "InvalidGroup.NotFound":
                        raise
        # Delete key pair + local file
        _purge_keypair()

def purge_artifacts():
    """Standalone: remove SG (if unused) and key pair + local PEM."""
    delete("both", purge=True)

def main():
    ap = argparse.ArgumentParser(description="EC2 nodes create/status/delete")
    ap.add_argument("action", choices=["create","status","delete","purge"])
    ap.add_argument("--which", choices=["public","private","both"], required=False, default="both")
    ap.add_argument("--purge", action="store_true", help="Also delete SSH SG (if unused) and key pair + local PEM")
    a = ap.parse_args()
    if a.action == "create":
        create(a.which)
    elif a.action == "status":
        status(a.which)
    elif a.action == "delete":
        delete(a.which, purge=a.purge)
    elif a.action == "purge":
        purge_artifacts()

if __name__ == "__main__":
    main()
