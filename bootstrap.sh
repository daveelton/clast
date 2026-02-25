#!/usr/bin/env bash
# Bootstrap a self-contained venv for clast inside the submodule directory.
# Usage: ./clast/bootstrap.sh        (from the parent project)
#    or: ./bootstrap.sh              (from the clast directory)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing clast dependencies ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$SCRIPT_DIR"

echo ""
echo "Done. venv python: $VENV_DIR/bin/python3"
echo ""
echo "To index your project:"
echo "  $VENV_DIR/bin/clang-ast-mcp index /path/to/project \\"
echo "    --compile-commands /path/to/build \\"
echo "    --db /path/to/project/.ast-index.db"