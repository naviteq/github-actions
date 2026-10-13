#!/usr/bin/env python3
"""Read a Terragrunt workflow config file: env_vars and credential profiles, resolved for one mode.

A profile value may be split by mode, `{plan: ..., apply: ...}`; the mode's half is kept and
a profile left with nothing but its name and paths is dropped. Without a file the inline
inputs pass through unchanged. It lives beside its action, not in scripts/, because the
public mirror copies actions only. → docs/terragrunt-plan.md

    python3 .github/actions/terragrunt-config/config.py --config-json config.json --mode plan
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

MODES = ("plan", "apply")
TOP_KEYS = {"env_vars", "profiles"}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FIXED = {"name", "paths"}


class ConfigError(ValueError):
    """A config file the workflow refuses to run with."""


def env_lines(raw: object) -> str:
    """KEY=VALUE lines, the shape of the workflows' env_vars input."""
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        raise ConfigError("env_vars must be a mapping of variable names to values")
    lines = []
    for key, value in raw.items():
        if not ENV_KEY.match(str(key)):
            raise ConfigError(f"env_vars: {key!r} is not a variable name")
        if isinstance(value, (dict, list)) or value is None:
            raise ConfigError(f"env_vars.{key} must be a string, number or boolean")
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if "\n" in text:
            raise ConfigError(f"env_vars.{key} must fit on one line")
        lines.append(f"{key}={text}")
    return "\n".join(lines)


def resolve(profile: object, mode: str, index: int) -> dict | None:
    """The profile as one mode sees it; None when nothing of it is left for that mode."""
    if not isinstance(profile, dict):
        raise ConfigError(f"profile {index + 1} is not a mapping")
    where = f"profile {profile.get('name') or index + 1}"
    resolved, split = {}, False
    for key, value in profile.items():
        if key in FIXED or not isinstance(value, dict):
            resolved[key] = value
            continue
        unknown = sorted(set(value) - set(MODES))
        if unknown or not value:
            raise ConfigError(f"{where}: {key} splits by mode, so its keys must be plan and apply, not "
                              + (", ".join(unknown) or "nothing"))
        split = True
        if mode in value:
            resolved[key] = value[mode]
    if split and set(resolved) <= FIXED:
        return None
    return resolved


def load(raw: object, mode: str) -> tuple[str, list[dict]]:
    """env_vars lines and the profiles for mode, from a parsed config file."""
    if mode not in MODES:
        raise ConfigError(f"mode must be plan or apply, not {mode!r}")
    if raw is None:
        return "", []
    if not isinstance(raw, dict):
        raise ConfigError("the config file must be a mapping with env_vars and profiles")
    unknown = sorted(set(raw) - TOP_KEYS)
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    profiles = raw.get("profiles")
    profiles = [] if profiles is None else profiles
    if not isinstance(profiles, list):
        raise ConfigError("profiles must be a list")
    resolved = [resolve(profile, mode, index) for index, profile in enumerate(profiles)]
    return env_lines(raw.get("env_vars")), [p for p in resolved if p is not None]


def write_outputs(**values: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in values.items():
                out.write(f"{key}<<CONFIG_EOF\n{value}\nCONFIG_EOF\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config-json", default="", help="The config file converted to JSON; empty for none")
    parser.add_argument("--mode", required=True)
    parser.add_argument("--profiles", default="", help="Inline profiles input, YAML")
    parser.add_argument("--env-vars", default="", help="Inline env_vars input, KEY=VALUE lines")
    args = parser.parse_args(argv)

    if not args.config_json:
        write_outputs(profiles=args.profiles, env_vars=args.env_vars)
        return 0
    if args.profiles.strip() or args.env_vars.strip():
        print("::error title=Two sources::config_file is set, so profiles and env_vars must not be given inline")
        return 1
    try:
        with open(args.config_json, encoding="utf-8") as handle:
            env_vars, profiles = load(json.load(handle), args.mode)
    except (ConfigError, json.JSONDecodeError) as error:
        print(f"::error title=Invalid config file::{error}")
        return 1
    print(f"{len(profiles)} profiles and {len(env_vars.splitlines())} env_vars for {args.mode}")
    write_outputs(profiles=json.dumps(profiles) if profiles else "", env_vars=env_vars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
