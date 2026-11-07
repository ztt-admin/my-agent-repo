#!/usr/bin/env bash
# Local CI: compile all modules and run the full unittest suite.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -m compileall -q brain tests
python3 -m unittest discover -s tests -v
