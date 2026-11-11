#!/bin/bash
# Run the MCP server tests.
#   ./run_tests.sh           unit tests only (fast, no network, no real data)
#   ./run_tests.sh --smoke   unit tests, then call server.py's tools on the real RSEc data
#                            (first run downloads ~62 MB into ./data and loads it)

set -euo pipefail
cd "$(dirname "$0")"

command -v uv >/dev/null || { echo "ERROR: uv is not installed (https://docs.astral.sh/uv/)"; exit 1; }

SMOKE=0
case "${1:-}" in
  "") ;;
  --smoke) SMOKE=1 ;;
  *) echo "Usage: $0 [--smoke]"; exit 2 ;;
esac

echo "=== Installing dependencies ==="
uv sync --quiet

echo "=== Unit tests ==="
uv run python -m unittest discover -s tests -t .

if [ "$SMOKE" -eq 1 ]; then
  echo ""
  echo "=== Smoke test: tools exposed by the server ==="
  uv run fastmcp list server.py
  echo ""
  echo "=== Smoke test: resolve_edam_term on the real data ==="
  uv run fastmcp call server.py resolve_edam_term text="sequence alignment" limit=2
  echo ""
  echo "=== Smoke test: search_software_by_operation (sequence alignment tools) ==="
  uv run fastmcp call server.py search_software_by_operation \
    --input-json '{"operation": "operation_0292", "limit": 2}'
  echo ""
  echo "=== Smoke test: get_software_io (clustalw) ==="
  uv run fastmcp call server.py get_software_io tool=clustalw
fi

echo ""
echo "=== All tests passed ==="
