#!/usr/bin/env python3
# infra_cc/deps.py
# Generic dependency framework + auto-loader for resource modules.

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

@dataclass
class Blocker:
    kind: str                 # e.g., "vpc", "subnet", "internet-gateway", "nat-gateway", "eni"
    id: str
    name: Optional[str] = None
    reason: Optional[str] = None
    children: List["Blocker"] = field(default_factory=list)

class DeleteBlocked(Exception):
    def __init__(self, root: Blocker, msg: str = "delete blocked by dependencies"):
        super().__init__(msg)
        self.root = root

# Registries
_CHECKERS: Dict[str, Callable[[str], List[Blocker]]] = {}
_DELETERS: Dict[str, Callable[[str], None]] = {}

# ---- Registration decorators ----
def register_checker(kind: str):
    def deco(fn: Callable[[str], List[Blocker]]):
        _CHECKERS[kind] = fn
        return fn
    return deco

def register_deleter(kind: str):
    def deco(fn: Callable[[str], None]):
        _DELETERS[kind] = fn
        return fn
    return deco

# ---- Auto-load all infra_cc.* modules once so their registrations apply ----
_PLUGINS_LOADED = False
def _ensure_plugins_loaded_once():
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    try:
        import pkgutil, importlib
        from . import __path__ as pkg_path, __name__ as pkg_name
        for m in pkgutil.iter_modules(pkg_path):
            name = m.name
            # Skip private helpers and ourselves
            if name.startswith("_") or name in ("deps",):
                continue
            importlib.import_module(f"{pkg_name}.{name}")
    finally:
        _PLUGINS_LOADED = True

# ---- Tree building / printing ----
def expand(kind: str, rid: str) -> List[Blocker]:
    fn = _CHECKERS.get(kind)
    return fn(rid) if fn else []

def _expand_recursive(node: Blocker) -> None:
    node.children = expand(node.kind, node.id)
    for ch in node.children:
        _expand_recursive(ch)

def build_tree(kind: str, rid: str, name: Optional[str] = None, reason: Optional[str] = None) -> Blocker:
    _ensure_plugins_loaded_once()
    root = Blocker(kind=kind, id=rid, name=name, reason=reason)
    _expand_recursive(root)
    return root

def print_tree(root: Blocker, indent: int = 0) -> None:
    pad = "  " * indent
    meta = []
    if root.name:
        meta.append(f"name={root.name}")
    if root.reason:
        meta.append(f"reason={root.reason}")
    extra = ("  " + "  ".join(meta)) if meta else ""
    print(f"{pad}- {root.kind}: {root.id}{extra}")
    for ch in root.children:
        print_tree(ch, indent + 1)

# ---- Deletion (post-order) ----
def _collect_missing_deleters(node: Blocker, missing: Optional[set] = None) -> set:
    if missing is None:
        missing = set()
    if node.kind not in _DELETERS:
        missing.add(node.kind)
    for ch in node.children:
        _collect_missing_deleters(ch, missing)
    return missing

def _delete_tree_postorder(node: Blocker) -> None:
    for ch in node.children:
        _delete_tree_postorder(ch)
    deleter = _DELETERS.get(node.kind)
    if not deleter:
        raise DeleteBlocked(node, msg=f"No deleter registered for kind '{node.kind}'")
    deleter(node.id)

def prompt_and_delete(root: Blocker, delete_root: bool = True) -> None:
    """
    Print the dependency tree and ask to delete blockers in order.
    If there are NO dependencies:
      - delete immediately (no prompt) when delete_root=True,
      - or do nothing (there's nothing to delete) when delete_root=False.
    """
    _ensure_plugins_loaded_once()

    # If no dependencies, fast-path: no prompt.
    if not root.children:
        if delete_root:
            deleter = _DELETERS.get(root.kind)
            if not deleter:
                raise DeleteBlocked(root, msg=f"Missing deleter for kind: {root.kind}")
            deleter(root.id)  # root-only delete, no prompt needed
        return

    # Dependencies exist: show tree and prompt
    print("[dependencies]")
    print_tree(root)

    # Ensure we have deleters for everything we'll touch
    missing = _collect_missing_deleters(root)
    if missing:
        if not delete_root and root.kind in missing:
            missing.remove(root.kind)
        if missing:
            kinds = ", ".join(sorted(missing))
            raise DeleteBlocked(root, msg=f"Missing deleter(s) for kind(s): {kinds}")

    ans = input("Delete these blockers in the correct order now? [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        raise DeleteBlocked(root, msg="User declined auto-delete of dependencies")

    # Delete children (and optionally root) post-order
    if delete_root:
        _delete_tree_postorder(root)
    else:
        for ch in root.children:
            _delete_tree_postorder(ch)
