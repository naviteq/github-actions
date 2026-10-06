#!/usr/bin/env python3
"""Split the selected Terragrunt units into credential profiles, one plan job per profile.

A profile maps path globs to the identity its units run under: an AWS role and region,
and the keys of terragrunt_extra_env it may see. A unit no profile matches fails the run.
It lives beside its action, not in scripts/, because the public mirror copies actions
only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-plan-profiles/match_profiles.py \\
        --profiles-json profiles.json --units "$UNITS" --deleted "$DELETED" --aws-region eu-central-1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEYS = {"name", "paths", "aws_role", "aws_region", "env", "skip"}


class ProfileError(ValueError):
    """A profiles input the workflow refuses to run with."""


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """`**` crosses directories, `*` and `?` stay within one path segment."""
    glob = glob.strip().removeprefix("./").rstrip("/")
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def load_profiles(raw: object) -> list[dict]:
    """Validate the profiles list; typos and missing fields fail here, not mid-plan."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProfileError("profiles must be a list")
    profiles = []
    seen = set()
    for index, item in enumerate(raw):
        where = f"profile {index + 1}"
        if not isinstance(item, dict):
            raise ProfileError(f"{where} is not a mapping")
        unknown = sorted(set(item) - KEYS)
        if unknown:
            raise ProfileError(f"{where} has unknown keys: {', '.join(unknown)}")
        name = item.get("name")
        if not isinstance(name, str) or not NAME.match(name):
            raise ProfileError(f"{where} needs a name of lowercase letters, digits and dashes")
        where = f"profile {name}"
        if name in seen:
            raise ProfileError(f"{where} is defined twice")
        seen.add(name)
        paths = item.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not paths or not all(isinstance(p, str) and p.strip() for p in paths):
            raise ProfileError(f"{where} needs at least one path glob")
        env = item.get("env") or []
        if not isinstance(env, list) or not all(isinstance(k, str) and ENV_KEY.match(k) for k in env):
            raise ProfileError(f"{where}: env must be a list of variable names")
        role = item.get("aws_role") or ""
        if not isinstance(role, str) or (role and not role.startswith("arn:")):
            raise ProfileError(f"{where}: aws_role must be a role ARN")
        skip = item.get("skip")
        if skip is not None and (not isinstance(skip, str) or not skip.strip()):
            raise ProfileError(f"{where}: skip must say why the units are not planned")
        profiles.append({
            "name": name,
            "patterns": [glob_to_regex(p) for p in paths],
            "aws_role": role,
            "aws_region": str(item.get("aws_region") or ""),
            "env": env,
            "skip": (skip or "").strip(),
        })
    return profiles


def match(unit: str, profiles: list[dict]) -> dict | None:
    """The first profile with a glob matching the unit, in the order they are listed."""
    for profile in profiles:
        if any(pattern.match(unit) for pattern in profile["patterns"]):
            return profile
    return None


def split(
    units: list[str], deleted: list[str], profiles: list[dict], default_region: str
) -> tuple[list[dict], list[tuple[str, str, str]], list[str]]:
    """Plan legs, skipped units (unit, profile, reason), and units no profile matches."""
    if not profiles:
        if not units and not deleted:
            return [], [], []
        leg = {"name": "default", "units": "\n".join(units), "deleted": "\n".join(deleted),
               "aws_role": "", "aws_region": default_region, "env": [], "env_all": True}
        return [leg], [], []

    legs: dict[str, dict] = {}
    skipped: list[tuple[str, str, str]] = []
    unmatched: list[str] = []
    for unit, kind in [(u, "units") for u in units] + [(u, "deleted") for u in deleted]:
        profile = match(unit, profiles)
        if profile is None:
            unmatched.append(unit)
            continue
        if profile["skip"]:
            skipped.append((unit, profile["name"], profile["skip"]))
            continue
        leg = legs.setdefault(profile["name"], {
            "name": profile["name"], "units": [], "deleted": [],
            "aws_role": profile["aws_role"], "aws_region": profile["aws_region"] or default_region,
            "env": profile["env"], "env_all": False,
        })
        leg[kind].append(unit)
    ordered = [legs[p["name"]] for p in profiles if p["name"] in legs]
    for leg in ordered:
        leg["units"] = "\n".join(leg["units"])
        leg["deleted"] = "\n".join(leg["deleted"])
    return ordered, skipped, unmatched


def summary(legs: list[dict], skipped: list[tuple[str, str, str]], unmatched: list[str]) -> str:
    lines = ["### Credential profiles", "", "| Unit | Profile |", "|---|---|"]
    for leg in legs:
        for unit in [u for u in leg["units"].splitlines() if u]:
            lines.append(f"| `{unit}` | {leg['name']} |")
        for unit in [u for u in leg["deleted"].splitlines() if u]:
            lines.append(f"| `{unit}` (deleted) | {leg['name']} |")
    for unit, name, reason in skipped:
        lines.append(f"| `{unit}` | {name}: not planned, {reason} |")
    for unit in unmatched:
        lines.append(f"| `{unit}` | **no profile** |")
    return "\n".join(lines) + "\n"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profiles-json", default="", help="File holding the profiles as JSON")
    parser.add_argument("--units", default="")
    parser.add_argument("--deleted", default="")
    parser.add_argument("--aws-region", default="")
    args = parser.parse_args(argv)

    try:
        raw = None
        if args.profiles_json:
            with open(args.profiles_json, encoding="utf-8") as handle:
                raw = json.load(handle)
        profiles = load_profiles(raw)
    except (ProfileError, json.JSONDecodeError) as error:
        print(f"::error title=Invalid profiles::{error}")
        return 1

    legs, skipped, unmatched = split(_lines(args.units), _lines(args.deleted), profiles, args.aws_region)
    if profiles:
        report = summary(legs, skipped, unmatched)
        print(report, end="")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
                out.write(report)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"legs={json.dumps(legs, separators=(',', ':'))}\n")
            out.write("skipped<<PLAN_PROFILES_EOF\n"
                      + "".join(f"{unit}\t{name}\t{reason}\n" for unit, name, reason in skipped)
                      + "PLAN_PROFILES_EOF\n")
    if unmatched:
        print(f"::error title=Units without a credential profile::{', '.join(unmatched)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
