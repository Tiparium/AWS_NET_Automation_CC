# infra_cc/session.py
# Purpose:
#   Central place to build a boto3 Session pinned to your class profile/region.
#   Verifies you're in the expected AWS account (safety).
#   Exposes helpers for clients/resources.
#
# Notes:
#   - This module NEVER creates/modifies resources in its self-test.
#     It only queries identity (STS) and lists basic info.
#   - Keep constants in one place to avoid drift across scripts.

from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# --- Your class context (edit here if these ever change) ---
PROFILE = "cloud_computing_CC"     # named AWS CLI profile you configured
REGION  = "us-west-1"              # default region for this project
ACCOUNT = "049930841222"           # hard safety guard (your account id)

# --- Internal singletons/flags ---
_SESSION_SINGLETON: boto3.Session | None = None
_PRINTED = False


def session() -> boto3.Session:
    """
    Return a singleton boto3 Session pinned to PROFILE/REGION.
    On first call:
      - Verifies the current caller identity matches ACCOUNT.
      - Prints a one-line context banner for human sanity.
    """
    global _SESSION_SINGLETON, _PRINTED

    if _SESSION_SINGLETON is None:
        # Create a new session explicitly tied to profile+region.
        s = boto3.Session(profile_name=PROFILE, region_name=REGION)

        # Safety: verify account once (no side effects; read-only STS call).
        try:
            sts = s.client("sts")
            ident = sts.get_caller_identity()  # {Account, Arn, UserId}
            acct = ident["Account"]
        except (BotoCoreError, ClientError) as e:
            raise SystemExit(f"[abort] Could not obtain caller identity via STS: {e}") from e

        if not _PRINTED:
            print(f"[ctx] profile={PROFILE} region={REGION} account={acct}")
            _PRINTED = True

        if acct != ACCOUNT:
            # Hard-stop: you're about to work in the wrong account.
            raise SystemExit(f"[abort] Wrong AWS account: {acct} (expected {ACCOUNT})")

        _SESSION_SINGLETON = s

    return _SESSION_SINGLETON


def client(service_name: str):
    """
    Shorthand: get a low-level client (dict-style API) for any service.
    Example: ec2 = client('ec2'); s3 = client('s3')
    """
    return session().client(service_name)


def resource(service_name: str):
    """
    Shorthand: get a high-level resource object (object-style API) where supported.
    Example: s3r = resource('s3'); ddb = resource('dynamodb')
    """
    return session().resource(service_name)


def ec2():
    """Common convenience: EC2 client (covers VPC/subnets/IGW/route tables)."""
    return client("ec2")


# -------------------- Self-test harness --------------------
def _self_test() -> int:
    """
    Minimal, read-only checks to confirm the session wiring:
      1) STS identity (already printed in session()).
      2) Region echo.
      3) EC2 'describe_regions' (lists region names; safe read).
      4) EC2 'describe_vpcs' with a tiny page size (safe read).
    Returns 0 on success, non-zero on failure.
    """
    try:
        s = session()  # triggers context print + account guard
        print(f"[ok] boto3 profile={s.profile_name} region={s.region_name}")

        ec2c = s.client("ec2")

        # 1) Regions list (prove we can talk to EC2)
        regs = ec2c.describe_regions(AllRegions=False)["Regions"]
        names = sorted(r["RegionName"] for r in regs)
        print(f"[ok] reachable regions example (subset) = {names[:5]}{' …' if len(names)>5 else ''}")

        # 2) VPC list (read-only; zero impact)
        vpcs = ec2c.describe_vpcs(MaxResults=5).get("Vpcs", [])
        print(f"[ok] sample VPC count in {s.region_name}: {len(vpcs)} (showing up to 5)")

        return 0
    except (BotoCoreError, ClientError) as e:
        print(f"[fail] self-test error: {e}")
        return 2


if __name__ == "__main__":
    # Allow:  python -m infra_cc.session
    raise SystemExit(_self_test())
