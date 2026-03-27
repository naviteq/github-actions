#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
CURRENT_SHA="$(git rev-parse HEAD)"
TARGET_PREFIX="naviteq/github-actions/"

cd "$REPO_ROOT"

if [[ "$#" -eq 0 ]]; then
  exit 0
fi

for file in "$@"; do
  [[ -f "$file" ]] || continue

  # Only touch workflow/action YAML files where uses: entries are expected.
  case "$file" in
    .github/workflows/*.yml|.github/workflows/*.yaml|.github/actions/*/action.yml|.github/actions/*/action.yaml)
      ;;
    *)
      continue
      ;;
  esac

  perl -i -pe "s#(uses:\\s+${TARGET_PREFIX}.+?)@[[:alnum:]._-]+#\$1\\@${CURRENT_SHA}#g" "$file"
done
