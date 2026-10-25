"""Store a secret in OCI Vault from a run: the mechanics shared by the OCI registry and receiver actions.

Best effort: nothing here raises to its caller or fails a run, and a refused call only warns. A value
reaches the oci CLI only through a private --from-json file removed afterwards; the CLI's own output
is captured and never printed. A secret is created when absent, updated when its value differs, left
alone when equal, and never touched in any state but ACTIVE. Refused calls run metadata-only
diagnostics. It lives beside its action because the public mirror copies actions only.
→ docs/terragrunt-apply-fresh.md

    import oci_vault
    vault = oci_vault.resolve_vault_ocid(vault_ocid, vault_unit_dir)
    outcome = oci_vault.store_secret(name, payload, same, repository, vault, region)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Callable

VAULT_OCID_RE = re.compile(r"ocid1\.vault\.[a-z0-9]+\.[a-z0-9-]*\..{1,128}")
SECRET_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")
REGION_RE = re.compile(r"[a-z]{2,}-[a-z0-9-]+-[0-9]+")
ERROR_CODE_RE = re.compile(r'"code":\s*"([A-Za-z]{1,64})"')
ERROR_STATUS_RE = re.compile(r'"status":\s*([0-9]{3})')
OCI_TIMEOUT = 120
TERRAGRUNT_TIMEOUT = 600
PREFERRED_KEY = "naviteq-registry-aes"
MANAGED_BY_TAG = "runestra_managed_by"
MANAGED_BY = "runestra-run"
REPOSITORY_TAG = "runestra_repository"
TITLE = "oci-vault"
LAST_ERROR = {"class": ""}
REFUSED_HINT = ("OCI answers 'Authorization failed or requested resource not found' for a missing grant "
                "and a missing secret alike")


def warn(reason: str, title: str = TITLE) -> None:
    print(f"::warning title={title}::{reason}", flush=True)


def mask(text: str) -> None:
    """One add-mask per line: a workflow command ends at a newline, so the rest would print."""
    for line in text.splitlines():
        if line.strip():
            print(f"::add-mask::{line}", flush=True)


def summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def report(line: str) -> None:
    print(line, flush=True)
    summary(line)


def oci(args: list[str], region: str) -> tuple[int, str, str]:
    """Run one oci call in the environment's region; (returncode, stdout, stderr), never printed here."""
    try:
        result = subprocess.run(
            ["oci", *args, "--region", region],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=OCI_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        LAST_ERROR["class"] = "the oci CLI did not run"
        return -1, "", ""
    if result.returncode != 0:
        LAST_ERROR["class"] = error_class(result.stderr)
    return result.returncode, result.stdout, result.stderr


def error_class(stderr: str) -> str:
    """The service error code and status only; nothing else of the CLI's output is repeated."""
    code = ERROR_CODE_RE.search(stderr)
    status = ERROR_STATUS_RE.search(stderr)
    if code and status:
        return f"{code.group(1)} ({status.group(1)})"
    return code.group(1) if code else "no service error code"


def document(stdout: str) -> object | None:
    """The first JSON document the CLI printed; {} for no output, which is how it prints an empty list."""
    text = stdout.lstrip()
    if not text:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(text)
        return value
    except ValueError:
        return None


def data_of(stdout: str) -> object | None:
    parsed = document(stdout)
    return parsed.get("data") if isinstance(parsed, dict) else None


def valid_vault_ocid(value: str) -> bool:
    return bool(VAULT_OCID_RE.fullmatch(value))


def valid_secret_name(value: str) -> bool:
    return bool(SECRET_NAME_RE.fullmatch(value))


def valid_region(value: str) -> bool:
    return bool(REGION_RE.fullmatch(value))


def resolve_vault_ocid(vault_ocid: str, vault_unit_dir: str, *, title: str = TITLE) -> str | None:
    """The shared vault when given, else the vault unit's own output; None after one warning."""
    if vault_ocid:
        if valid_vault_ocid(vault_ocid):
            return vault_ocid
        warn("The vault OCID is not an OCI vault OCID; nothing was stored", title)
        return None
    if not vault_unit_dir:
        warn("No vault OCID and no vault unit directory; nothing was stored", title)
        return None
    if shutil.which("terragrunt") is None:
        warn("No vault OCID and no terragrunt to read the vault unit output; nothing was stored", title)
        return None
    try:
        result = subprocess.run(
            ["terragrunt", "run", "--working-dir", vault_unit_dir, "--", "output", "-raw", "vault_id"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TERRAGRUNT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        warn("Could not read the vault unit output vault_id: terragrunt did not run; nothing was stored", title)
        return None
    if result.returncode != 0:
        warn("Could not read the vault unit output vault_id: terragrunt failed; nothing was stored", title)
        return None
    answer = result.stdout.strip()
    if not valid_vault_ocid(answer):
        warn("The vault unit output vault_id is empty or not a vault OCID; nothing was stored", title)
        return None
    return answer


def vault_facts(vault_ocid: str, region: str) -> tuple[str, str] | None:
    """(compartment_id, management_endpoint) of the vault, or None when it cannot be read."""
    code, stdout, _ = oci(["kms", "management", "vault", "get", "--vault-id", vault_ocid], region)
    data = data_of(stdout) if code == 0 else None
    if not isinstance(data, dict):
        return None
    compartment, endpoint = data.get("compartment-id"), data.get("management-endpoint")
    if not isinstance(compartment, str) or not isinstance(endpoint, str) or not compartment or not endpoint:
        return None
    return compartment, endpoint


def pick_key(endpoint: str, compartment_id: str, region: str) -> str | None:
    """The ENABLED AES key named naviteq-registry-aes, else the first ENABLED AES key, else None."""
    code, stdout, _ = oci(["kms", "management", "key", "list", "--endpoint", endpoint,
                           "--compartment-id", compartment_id, "--algorithm", "AES", "--all"], region)
    data = data_of(stdout) if code == 0 else None
    if not isinstance(data, list):
        return None
    enabled = [key for key in data if isinstance(key, dict) and key.get("lifecycle-state") == "ENABLED"
               and isinstance(key.get("id"), str)]
    for key in enabled:
        if key.get("display-name") == PREFERRED_KEY:
            return key["id"]
    return enabled[0]["id"] if enabled else None


def describe_secret(name: str, vault_ocid: str, compartment_id: str,
                    region: str) -> tuple[str, str | None, dict[str, str]]:
    """("absent"|"active"|"inactive:<STATE>"|"unknown", secret_id, freeform tags) for the name."""
    code, stdout, _ = oci(["vault", "secret", "list", "--compartment-id", compartment_id,
                           "--vault-id", vault_ocid, "--name", name, "--all"], region)
    parsed = document(stdout) if code == 0 else None
    data = parsed.get("data", []) if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        return "unknown", None, {}
    matches = [entry for entry in data if isinstance(entry, dict) and entry.get("secret-name") == name
               and entry.get("lifecycle-state") != "DELETED"]
    if not matches:
        return "absent", None, {}
    chosen = next((entry for entry in matches if entry.get("lifecycle-state") == "ACTIVE"), matches[0])
    tags = chosen.get("freeform-tags") if isinstance(chosen.get("freeform-tags"), dict) else {}
    tags = {str(key): str(value) for key, value in tags.items()}
    secret_id = chosen.get("id") if isinstance(chosen.get("id"), str) else None
    state = str(chosen.get("lifecycle-state") or "UNKNOWN")
    if state == "ACTIVE" and secret_id:
        return "active", secret_id, tags
    return f"inactive:{state}", secret_id, tags


def read_secret(name: str, vault_ocid: str, region: str) -> str | None:
    """The current value of the secret, for comparison only; None on any failure. Never print it."""
    code, stdout, _ = oci(["secrets", "secret-bundle", "get-secret-bundle-by-name", "--secret-name", name,
                           "--vault-id", vault_ocid, "--query", 'data."secret-bundle-content".content',
                           "--raw-output"], region)
    lines = stdout.strip().splitlines()
    if code != 0 or not lines:
        return None
    try:
        return base64.b64decode(lines[0].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None


def write_json(args: list[str], body: dict[str, object], region: str) -> bool:
    """Run a write with its body in a mode-0600 --from-json file; True when it succeeded."""
    handle, path = tempfile.mkstemp(prefix="oci-vault-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(body, file)
        code, _, _ = oci([*args, "--from-json", f"file://{path}"], region)
        return code == 0
    except (OSError, ValueError):
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def encoded(payload: str) -> str:
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def create_secret(name: str, payload: str, vault_ocid: str, compartment_id: str, key_id: str, repository: str,
                  region: str, description: str) -> bool:
    tags = {MANAGED_BY_TAG: MANAGED_BY}
    if repository:
        tags[REPOSITORY_TAG] = repository
    body = {"compartmentId": compartment_id, "vaultId": vault_ocid, "keyId": key_id, "secretName": name,
            "secretContentContent": encoded(payload), "description": description, "freeformTags": tags}
    return write_json(["vault", "secret", "create-base64"], body, region)


def update_secret(secret_id: str, payload: str, region: str) -> bool:
    body = {"secretId": secret_id, "secretContentContent": encoded(payload)}
    return write_json(["vault", "secret", "update-base64"], body, region)


def diagnose(vault_ocid: str, compartment_id: str, region: str) -> None:
    """Metadata only: secret names and states in the vault, policies on its compartment."""
    try:
        lines = ["OCI Vault diagnostics (names, states and policy statements only):"]
        code, stdout, _ = oci(["vault", "secret", "list", "--compartment-id", compartment_id,
                               "--vault-id", vault_ocid, "--all"], region)
        parsed = document(stdout) if code == 0 else None
        secrets = parsed.get("data", []) if isinstance(parsed, dict) else None
        if isinstance(secrets, list):
            lines.append(f"Secrets in the vault: {len(secrets)}")
            lines += [f"- {entry.get('secret-name')}: {entry.get('lifecycle-state')}"
                      for entry in secrets if isinstance(entry, dict)]
        else:
            lines.append("Secrets in the vault: could not list them")
        code, stdout, _ = oci(["iam", "policy", "list", "--compartment-id", compartment_id, "--all"], region)
        parsed = document(stdout) if code == 0 else None
        policies = parsed.get("data", []) if isinstance(parsed, dict) else None
        if isinstance(policies, list):
            lines.append(f"Policies on the vault compartment: {len(policies)}")
            for policy in policies:
                if isinstance(policy, dict):
                    lines.append(f"- {policy.get('name')}")
                    lines += [f"  - {statement}" for statement in policy.get("statements") or []]
        else:
            lines.append("Policies on the vault compartment: could not list them")
        print("::group::OCI Vault diagnostics", flush=True)
        for line in lines:
            report(line)
        print("::endgroup::", flush=True)
    except Exception:  # noqa: BLE001 - diagnostics never fail the store
        pass


def store_secret(name: str, payload: str, same: Callable[[str, str], bool], repository: str, vault_ocid: str,
                 region: str, *, subject: str = "Secret", title: str = TITLE,
                 description: str = "Written by the Runestra run") -> str:
    """created, unchanged, updated or skipped; every skip has warned already."""
    facts = vault_facts(vault_ocid, region)
    if facts is None:
        warn(f"{subject} was not stored: could not read vault {vault_ocid} in {region}", title)
        return "skipped"
    compartment_id, endpoint = facts
    tried_create = False
    while True:
        state, secret_id, tags = describe_secret(name, vault_ocid, compartment_id, region)
        if state == "unknown":
            warn(f"{subject} was not stored: could not list {name} in the vault ({LAST_ERROR['class']}); "
                 f"{REFUSED_HINT}", title)
            diagnose(vault_ocid, compartment_id, region)
            return "skipped"
        if state.startswith("inactive:"):
            warn(f"{subject} was not stored: {name} is scheduled for deletion or not active "
                 f"({state.split(':', 1)[1]}); nothing was written", title)
            diagnose(vault_ocid, compartment_id, region)
            return "skipped"
        if state == "active" and secret_id:
            current = read_secret(name, vault_ocid, region)
            if current is not None and same(current, payload):
                return "unchanged"
            if not update_secret(secret_id, payload, region):
                warn(f"{subject} was not stored: OCI refused the update of {name} ({LAST_ERROR['class']}); "
                     f"{REFUSED_HINT}", title)
                diagnose(vault_ocid, compartment_id, region)
                return "skipped"
            previous = tags.get(REPOSITORY_TAG, "")
            if previous and repository and previous != repository:
                line = (f"{subject} {name} was last written by another repository ({previous}); "
                        "overwritten with this repository's value")
                warn(line, title)
                summary(line)
            return "updated"
        if tried_create:
            warn(f"{subject} was not stored: OCI refused to create {name} ({LAST_ERROR['class']}); "
                 f"{REFUSED_HINT}", title)
            diagnose(vault_ocid, compartment_id, region)
            return "skipped"
        key_id = pick_key(endpoint, compartment_id, region)
        if key_id is None:
            warn(f"{subject} was not stored: no ENABLED AES key in vault {vault_ocid}", title)
            return "skipped"
        if create_secret(name, payload, vault_ocid, compartment_id, key_id, repository, region, description):
            return "created"
        tried_create = True
