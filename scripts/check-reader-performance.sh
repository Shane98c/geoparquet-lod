#!/usr/bin/env bash
set -euo pipefail
# Build the base in a temporary directory; never switch the working checkout.
root=$(git rev-parse --show-toplevel)
base=${1:-HEAD}
task_tmp=$(mktemp -d)
trap 'rm -rf "$task_tmp"' EXIT
git -C "$root" archive "$base" js | tar -x -C "$task_tmp"
ln -s "$root/js/node_modules" "$task_tmp/js/node_modules"
(cd "$task_tmp/js" && npm run build) >&2
(cd "$root/js" && npm run build) >&2
(cd "$root" && uv run python examples/quickstart.py "$task_tmp/fixture") >&2
fixture="$task_tmp/fixture/buildings.lod.parquet"
node "$root/js/test/performance-gate.mjs" "$task_tmp/js/dist" "$root/js/dist" "$fixture"
