#!/usr/bin/env bash
# Bootstrap a self-contained venv for clast inside the submodule directory.
# Usage: ./clast/bootstrap.sh        (from the parent project)
#    or: ./bootstrap.sh              (from the clast directory)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_MD="$PROJECT_DIR/CLAUDE.md"
ADDITION="$SCRIPT_DIR/CLAUDE-CLAST-ADDITION.md"

# ── Create venv and install dependencies ────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating venv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing clast dependencies ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$SCRIPT_DIR"

echo ""
echo "Done. venv python: $VENV_DIR/bin/python3"

# ── Offer to update CLAUDE.md ──────────────────────────────────────
echo ""
if [ -f "$CLAUDE_MD" ]; then
    if grep -q "ast_search" "$CLAUDE_MD" 2>/dev/null; then
        echo "CLAUDE.md already contains clast instructions — skipping."
    else
        echo "Found $CLAUDE_MD"
        read -rp "Append clast instructions to CLAUDE.md? [Y/n] " answer
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            echo "" >> "$CLAUDE_MD"
            cat "$ADDITION" >> "$CLAUDE_MD"
            echo "Updated CLAUDE.md with clast instructions."
        else
            echo "Skipped. You can add them manually — see:"
            echo "  $ADDITION"
        fi
    fi
else
    echo "No CLAUDE.md found at $PROJECT_DIR"
    echo "To help Claude Code use the AST index, add the contents of"
    echo "  $ADDITION"
    echo "to your project's CLAUDE.md."
fi