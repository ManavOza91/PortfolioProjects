#!/usr/bin/env bash
# Signal Engine — one command.
#
#   ./run.sh                  migrate, seed, rescore, open the app
#   ./run.sh import --dry-run preview a CSV import
#   ./run.sh seed             reload data/taxonomy.yaml
#   ./run.sh rescore          recompute all scores
#   ./run.sh demo             load a worked example
#   ./run.sh test             run the tests
#   ./run.sh purge-contacts   delete all personal data
#
# Everything else is passed straight through to the app.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  cat <<'EOF'
uv is not installed. It is the only prerequisite.

  macOS / Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh
  Windows:        powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

Then run ./run.sh again.
EOF
  exit 1
fi

# Create the environment on first run, and keep it in step with pyproject.toml
# after that. Quiet unless something actually changes.
# The test suite needs a few extra packages; nothing else does.
EXTRAS="."
if [ "${1:-}" = "test" ]; then
  EXTRAS=".[dev]"
fi

if [ ! -d .venv ]; then
  echo "First run — setting up. This takes a minute."
  uv venv --python 3.11 >/dev/null 2>&1
  uv pip install -e "$EXTRAS" --quiet
elif [ pyproject.toml -nt .venv/pyvenv.cfg ]; then
  echo "Dependencies changed — updating."
  uv pip install -e "$EXTRAS" --quiet
  touch .venv/pyvenv.cfg
elif [ "$EXTRAS" != "." ] && [ ! -x .venv/bin/pytest ]; then
  echo "Installing test dependencies."
  uv pip install -e "$EXTRAS" --quiet
fi

if [ ! -f .env ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Note: no ANTHROPIC_API_KEY found. The app runs, but note parsing is off."
  echo "      Put ANTHROPIC_API_KEY=sk-ant-... in a .env file here to switch it on."
  echo
fi

# Scripts that live outside the app package.
if [ "${1:-}" = "parser-check" ]; then
  shift
  exec .venv/bin/python scripts/parser_check.py "$@"
fi
if [ "${1:-}" = "export-portable" ]; then
  shift
  exec .venv/bin/python scripts/export_portable.py "$@"
fi

exec .venv/bin/python -m signal_engine "$@"
