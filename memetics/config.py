"""Validate standing authorizations before storing or executing them."""

import hashlib
import json
import re
from pathlib import PurePosixPath


class Blocked(Exception):
    """A decision requiring configuration or human reconciliation, not a retry."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value
    ):
        raise ValueError("repository must be owner/name")
    if any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("invalid repository")
    return value


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(p in {"..", ".git"} for p in path.parts)
        or str(path) == "."
    ):
        raise ValueError("path must remain inside the checkout and outside .git")
    return value


def in_scope(path, scopes):
    return any(
        path == scope.rstrip("/") or (scope.endswith("/") and path.startswith(scope))
        for scope in scopes
    )


def validate(config):
    c = json.loads(canonical(config))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", c.get("id", "")):
        raise ValueError(
            "listener id must contain 1-80 letters, numbers, underscores or hyphens"
        )
    if not isinstance(c.get("enabled", True), bool):
        raise ValueError("enabled must be boolean")
    c.setdefault("enabled", True)
    if not isinstance(c.get("concern"), str) or not c["concern"].strip():
        raise ValueError("concern is required")
    for section, ref_key in [("upstream", "ref"), ("destination", "base_ref")]:
        value = c[section]
        value["repository"] = repo_name(value["repository"]).lower()
        ref = value[ref_key]
        if (
            not isinstance(ref, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", ref)
            or ".." in ref
            or "//" in ref
            or ref.endswith(("/", ".lock", "."))
        ):
            raise ValueError("invalid tracked branch")
        if not isinstance(value.get("paths"), list) or not value["paths"]:
            raise ValueError(f"{section}.paths is required")
        for path in value["paths"] + value.get("test_paths", []):
            safe_path(path)
    if not re.fullmatch(r"[a-f0-9]{40}", c["upstream"].get("baseline_commit", "")):
        raise ValueError(
            "upstream.baseline_commit must be an immutable 40-character SHA"
        )
    adaptation = c["adaptation"]
    if not isinstance(adaptation.get("instructions"), str):
        raise ValueError("adaptation.instructions is required")
    commands = adaptation.get("validation_commands")
    if (
        not isinstance(commands, list)
        or not 1 <= len(commands) <= 10
        or any(not isinstance(x, str) or not x.strip() for x in commands)
    ):
        raise ValueError("at least one validation command is required")
    adaptation.setdefault("validation_image", "python:3.13-slim")
    if not isinstance(adaptation["validation_image"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", adaptation["validation_image"]
    ):
        raise ValueError("invalid validation image")
    adaptation.setdefault("timeout_seconds", 120)
    if (
        not isinstance(adaptation["timeout_seconds"], int)
        or not 1 <= adaptation["timeout_seconds"] <= 900
    ):
        raise ValueError("validation timeout must be 1-900 seconds")
    delivery = c.setdefault("delivery", {})
    if (
        delivery.get("auto_merge", False)
        or delivery.get("mode", "automatic_draft_pr") != "automatic_draft_pr"
    ):
        raise ValueError(
            "only automatic draft PR delivery is supported; auto-merge is forbidden"
        )
    if delivery.get("update_existing_pr", True) is not True:
        raise ValueError("update_existing_pr must be true")
    delivery.update(
        mode="automatic_draft_pr", auto_merge=False, update_existing_pr=True
    )
    return c
