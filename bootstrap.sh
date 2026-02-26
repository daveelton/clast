#!/usr/bin/env bash
# Bootstrap a self-contained venv for clast inside the clast directory.
# Usage: ./clast/bootstrap.sh        (from the parent project)
#    or: ./bootstrap.sh              (from the clast directory)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_MD="$PROJECT_DIR/CLAUDE.md"
ADDITION="$SCRIPT_DIR/CLAUDE-CLAST-ADDITION.md"

# Current version — bump this when CLAUDE-CLAST-ADDITION.md changes
CLAST_VERSION="v2"

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

# ── Update CLAUDE.md ──────────────────────────────────────────────

update_claude_md() {
    if [ ! -f "$CLAUDE_MD" ]; then
        echo ""
        echo "No CLAUDE.md found at $PROJECT_DIR"
        echo "To help Claude Code use the AST index, add the contents of"
        echo "  $ADDITION"
        echo "to your project's CLAUDE.md."
        return
    fi

    echo ""

    # Check if current version is already present
    if grep -q "clast-instructions $CLAST_VERSION" "$CLAUDE_MD" 2>/dev/null; then
        echo "CLAUDE.md already has clast instructions ($CLAST_VERSION) — up to date."
        return
    fi

    # Check if an older version exists
    if grep -q "clast-instructions" "$CLAUDE_MD" 2>/dev/null; then
        old_ver=$(grep -o 'clast-instructions v[0-9]*' "$CLAUDE_MD" | head -1 | grep -o 'v[0-9]*')
        echo "CLAUDE.md has outdated clast instructions (${old_ver:-unknown} → $CLAST_VERSION)."
        read -rp "Replace with updated version? [Y/n] " answer
        if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
            # Remove old block (everything between the markers, inclusive)
            sed -i.clast-bak '/<!-- clast-instructions/,/<!-- \/clast-instructions -->/d' "$CLAUDE_MD"
            cat "$ADDITION" >> "$CLAUDE_MD"
            rm -f "$CLAUDE_MD.clast-bak"
            echo "Updated clast instructions in CLAUDE.md."
        else
            echo "Skipped. You can update manually — see:"
            echo "  $ADDITION"
        fi
        return
    fi

    # Check for pre-marker clast content (from before versioning was added)
    if grep -q "ast_search" "$CLAUDE_MD" 2>/dev/null; then
        echo "CLAUDE.md has clast instructions but without version markers."
        echo "Please replace the clast section manually with the contents of:"
        echo "  $ADDITION"
        return
    fi

    # No clast content at all — offer to append
    echo "Found $CLAUDE_MD"
    read -rp "Append clast instructions to CLAUDE.md? [Y/n] " answer
    if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then
        echo "" >> "$CLAUDE_MD"
        cat "$ADDITION" >> "$CLAUDE_MD"
        echo "Added clast instructions to CLAUDE.md."
    else
        echo "Skipped. You can add them manually — see:"
        echo "  $ADDITION"
    fi
}

update_claude_md