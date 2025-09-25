# infra_cc/naming.py
# Purpose:
#   Centralize your naming convention and common AWS tags so every resource is consistent.
#   - Name format: nainoa-faulkner-jackson-<resource>_HW3_CC
#   - Stack tag groups all resources from this assignment for easy filtering.
#
# Keep ALL naming/tags here so you change them once if needed.

from __future__ import annotations
from typing import List, Dict

# Base parts of your convention
PREFIX = "nainoa-faulkner-jackson"
SUFFIX = "_HW3_CC"

# A stack/group tag that all resources share (handy for console filters & teardown)
STACK = f"{PREFIX}-stack{SUFFIX}"  # -> nainoa-faulkner-jackson-stack_HW3_CC

def res_name(resource: str) -> str:
    """
    Compose the canonical Name tag for a given resource type.
    Example:
      res_name("vpc")            -> nainoa-faulkner-jackson-vpc_HW3_CC
      res_name("subnet-public")  -> nainoa-faulkner-jackson-subnet-public_HW3_CC
    """
    # Normalize resource bits just a little (strip slashes/whitespace)
    clean = resource.strip().replace("/", "-")
    return f"{PREFIX}-{clean}{SUFFIX}"

def tags_for(name: str) -> List[Dict[str, str]]:
    """
    Return a standard set of tags to apply to all resources in this assignment.
    The 'Name' tag is what the AWS console shows as the display name.
    """
    return [
        {"Key": "Name",        "Value": name},
        {"Key": "Stack",       "Value": STACK},
        {"Key": "Project",     "Value": "Cloud_Computing"},
        {"Key": "Owner",       "Value": "Mars"},
        {"Key": "Environment", "Value": "dev"},
        # Add course/assignment identifiers if you want:
        {"Key": "Assignment",  "Value": "HW3"},
    ]

# -------------------- Self-test harness --------------------
def _self_test() -> int:
    """
    Prints sample names and tags so you can eyeball correctness.
    No AWS calls here—pure string logic.
    """
    samples = [
        "vpc",
        "subnet-public",
        "subnet-private",
        "igw",
        "rt-public",
        "rt-private",
        "natgw",
        "eip-nat",
    ]
    print("[naming] STACK =", STACK)
    for r in samples:
        n = res_name(r)
        t = tags_for(n)
        print(f"[naming] {r:14s} -> {n}")
        # Show first few tags compactly
        preview = ", ".join(f"{kv['Key']}={kv['Value']}" for kv in t[:3])
        print(f"          tags: {preview} ... (+{max(0,len(t)-3)} more)")
    return 0

if __name__ == "__main__":
    # Allow:  python -m infra_cc.naming
    raise SystemExit(_self_test())
