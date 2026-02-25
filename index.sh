#!/usr/bin/env bash
# Index the parent C++ project using clast.
# All output (.ast-index.db) stays inside the clast directory.
#
# Usage:
#   ./index.sh                                        # auto-find compile_commands.json
#   ./index.sh /path/to/cmake-build-debug             # explicit build dir
#   ./index.sh --force                                # re-index everything
#   ./index.sh /path/to/cmake-build-debug --force     # both
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DB_PATH="$SCRIPT_DIR/.ast-index.db"

# ── Check venv exists ────────────────────────────────────────────────
if [ ! -f "$VENV_DIR/bin/clang-ast-mcp" ]; then
    echo "Error: venv not found. Run bootstrap.sh first:"
    echo "  ./bootstrap.sh"
    exit 1
fi

# ── Parse arguments ──────────────────────────────────────────────────
COMPILE_COMMANDS_DIR=""
FORCE=""

for arg in "$@"; do
    if [ "$arg" = "--force" ] || [ "$arg" = "-f" ]; then
        FORCE="--force"
    else
        COMPILE_COMMANDS_DIR="$arg"
    fi
done

# ── Find compile_commands.json ───────────────────────────────────────
if [ -z "$COMPILE_COMMANDS_DIR" ]; then
    # Search common CLion/CMake build directories relative to the project
    CANDIDATES=(
        "$PROJECT_DIR/cmake-build-debug"
        "$PROJECT_DIR/cmake-build-release"
        "$PROJECT_DIR/cmake-build-relwithdebinfo"
        "$PROJECT_DIR/build"
    )
    for dir in "${CANDIDATES[@]}"; do
        if [ -f "$dir/compile_commands.json" ]; then
            COMPILE_COMMANDS_DIR="$dir"
            break
        fi
    done
fi

if [ -z "$COMPILE_COMMANDS_DIR" ] || [ ! -f "$COMPILE_COMMANDS_DIR/compile_commands.json" ]; then
    echo "Error: compile_commands.json not found."
    echo ""
    echo "Searched:"
    echo "  ../cmake-build-debug/"
    echo "  ../cmake-build-release/"
    echo "  ../cmake-build-relwithdebinfo/"
    echo "  ../build/"
    echo ""
    echo "Usage: ./index.sh /path/to/build-dir"
    echo ""
    echo "To generate it, add -DCMAKE_EXPORT_COMPILE_COMMANDS=ON to your CMake options."
    echo ""
    echo "  Command line:  cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
    echo ""
    echo "  CLion:         Settings > Build, Execution, Deployment > CMake"
    echo "                 Append to the 'CMake options' field, then rebuild."
    exit 1
fi

# ── Index ────────────────────────────────────────────────────────────
echo "Project:            $PROJECT_DIR"
echo "compile_commands:   $COMPILE_COMMANDS_DIR/compile_commands.json"
echo "Database:           $DB_PATH"
echo ""

"$VENV_DIR/bin/clang-ast-mcp" index "$PROJECT_DIR" \
    --compile-commands "$COMPILE_COMMANDS_DIR" \
    --db "$DB_PATH" \
    $FORCE