#!/usr/bin/env bash
# Bootstrap clast for a consumer project — opt-in and non-invasive by default.
#
# Everything it wires up is personal and NOT committed to the parent project:
#   - venv                 -> clast/.venv          (self-contained Python env)
#   - clast instructions   -> CLAUDE.local.md      (personal, gitignored)
#   - clang-ast MCP server -> Claude Code "local" scope (per-project user settings)
#   - clast/, CLAUDE.local.md added to the parent project's .gitignore
#
# This means a teammate who never runs bootstrap.sh sees zero clast footprint:
# no MCP server to fail, no instructions referencing tools they don't have.
#
# Usage: ./clast/bootstrap.sh        (from the parent project)
#    or: ./bootstrap.sh              (from the clast directory)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_LOCAL_MD="$PROJECT_DIR/CLAUDE.local.md"
CLAUDE_MD="$PROJECT_DIR/CLAUDE.md"
ADDITION="$SCRIPT_DIR/CLAUDE-CLAST-ADDITION.md"
GITIGNORE="$PROJECT_DIR/.gitignore"
# Path to clast relative to the project root (usually "clast"). The MCP serve
# command registered below MUST use this RELATIVE form, not absolute paths:
# Claude Code canonicalises every git worktree of a repo onto a single
# local-scope config key, so one clang-ast entry is shared by all worktrees.
# A relative path is resolved against each session's own working directory at
# launch, so that single shared entry gives each worktree its own
# clast/.venv + .ast-index.db. An absolute path would instead pin every
# worktree to whichever one last ran bootstrap. (The venv/pip steps further
# down still use the absolute $SCRIPT_DIR/$VENV_DIR — only the launched serve
# command needs to be relative.)
CLAST_REL="${SCRIPT_DIR#"$PROJECT_DIR"/}"

# Current version — bump this when CLAUDE-CLAST-ADDITION.md changes
CLAST_VERSION="v4"

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

# ── Ensure clast artefacts are gitignored in the parent project ─────
update_gitignore() {
    for entry in "clast/" "CLAUDE.local.md"; do
        if [ -f "$GITIGNORE" ] && grep -qxF "$entry" "$GITIGNORE" 2>/dev/null; then
            continue
        fi
        echo "$entry" >> "$GITIGNORE"
        echo "Added '$entry' to $GITIGNORE"
    done
}

# ── Write clast instructions to CLAUDE.local.md (personal, gitignored) ──
update_claude_md() {
    if [ ! -f "$CLAUDE_LOCAL_MD" ]; then
        cat "$ADDITION" > "$CLAUDE_LOCAL_MD"
        echo "Created $CLAUDE_LOCAL_MD with clast instructions."
        return
    fi

    if grep -q "clast-instructions $CLAST_VERSION" "$CLAUDE_LOCAL_MD" 2>/dev/null; then
        echo "CLAUDE.local.md already has clast instructions ($CLAST_VERSION) — up to date."
        return
    fi

    # Older marker present → replace the block in place.
    if grep -q "clast-instructions" "$CLAUDE_LOCAL_MD" 2>/dev/null; then
        old_ver=$(grep -o 'clast-instructions v[0-9]*' "$CLAUDE_LOCAL_MD" | head -1 | grep -o 'v[0-9]*')
        sed -i.clast-bak '/<!-- clast-instructions/,/<!-- \/clast-instructions -->/d' "$CLAUDE_LOCAL_MD"
        cat "$ADDITION" >> "$CLAUDE_LOCAL_MD"
        rm -f "$CLAUDE_LOCAL_MD.clast-bak"
        echo "Updated clast instructions in CLAUDE.local.md (${old_ver:-unknown} → $CLAST_VERSION)."
        return
    fi

    # No markers → append.
    printf '\n' >> "$CLAUDE_LOCAL_MD"
    cat "$ADDITION" >> "$CLAUDE_LOCAL_MD"
    echo "Appended clast instructions to CLAUDE.local.md."
}

# ── Register the clang-ast MCP server in Claude Code "local" scope ──
detect_libclang() {
    local libdir cand prefix
    if command -v llvm-config >/dev/null 2>&1; then
        libdir="$(llvm-config --libdir 2>/dev/null || true)"
        for cand in "$libdir/libclang.dylib" "$libdir/libclang.so"; do
            [ -f "$cand" ] && { echo "$cand"; return; }
        done
    fi
    if command -v brew >/dev/null 2>&1; then
        prefix="$(brew --prefix llvm 2>/dev/null || true)"
        for cand in "$prefix/lib/libclang.dylib" "$prefix/lib/libclang.so"; do
            [ -f "$cand" ] && { echo "$cand"; return; }
        done
    fi
    echo ""
}

register_mcp() {
    local libclang serve
    libclang="$(detect_libclang)"
    serve="./$CLAST_REL/.venv/bin/python3 -m clang_ast_mcp serve --db ./$CLAST_REL/.ast-index.db 2>>./$CLAST_REL/mcp.log"

    if ! command -v claude >/dev/null 2>&1; then
        echo ""
        echo "The 'claude' CLI was not found on PATH — MCP server not registered."
        echo "Register it yourself (local scope, not committed) with:"
        echo "  claude mcp add --scope local clang-ast -e LIBCLANG_PATH=${libclang:-/path/to/libclang.(dylib|so)} -- bash -c \"$serve\""
        return
    fi

    if [ -z "$libclang" ]; then
        echo ""
        echo "Could not auto-detect libclang — MCP server not registered."
        echo "Install it (brew install llvm / apt install libclang-dev), then run:"
        echo "  claude mcp add --scope local clang-ast -e LIBCLANG_PATH=/path/to/libclang.(dylib|so) -- bash -c \"$serve\""
        return
    fi

    # Re-runnable: drop any existing local-scope entry first.
    claude mcp remove --scope local clang-ast >/dev/null 2>&1 || true
    claude mcp add --scope local clang-ast -e "LIBCLANG_PATH=$libclang" -- bash -c "$serve"
    echo "Registered clang-ast MCP server (local scope), LIBCLANG_PATH=$libclang"
}

# ── Yes/no prompt, defaulting to yes; "no" when non-interactive ─────
confirm() {
    local reply
    # No TTY (CI, piped) → caller's safe fallback, never silently edit/build.
    [ -t 0 ] || return 1
    read -r -p "$1 [Y/n] " reply
    case "$reply" in
        [nN] | [nN][oO]) return 1 ;;
        *) return 0 ;;
    esac
}

# ── Remove a legacy clast block from the committed CLAUDE.md ─────────
# v4 keeps instructions in CLAUDE.local.md (gitignored); any clast block in
# the tracked CLAUDE.md is from an older version (v3 or earlier) and would
# impose clast on teammates if committed.
remove_legacy_claude_md() {
    [ -f "$CLAUDE_MD" ] && grep -q "clast-instructions" "$CLAUDE_MD" 2>/dev/null || return 0

    local old_ver
    old_ver=$(grep -o 'clast-instructions v[0-9]*' "$CLAUDE_MD" | head -1 | grep -o 'v[0-9]*')
    echo ""
    echo "Found a legacy clast block (${old_ver:-pre-v4}) in the committed $CLAUDE_MD."
    echo "v4 keeps its instructions in CLAUDE.local.md (gitignored), so this block is stale."

    if confirm "Remove the stale clast block from CLAUDE.md?"; then
        # Delete the marked block. A blank line where it sat may remain — harmless,
        # and the user is pointed at the diff to review anyway.
        sed -i.clast-bak \
            -e '/<!-- clast-instructions/,/<!-- \/clast-instructions -->/d' \
            "$CLAUDE_MD"
        rm -f "$CLAUDE_MD.clast-bak"
        echo "Removed. Review and commit it:  git diff -- $CLAUDE_MD"
    else
        echo "Left as-is. Remove the <!-- clast-instructions ... --> block from CLAUDE.md manually."
    fi
}

# ── Offer to build the AST index, and explain the ongoing workflow ──
offer_index() {
    local cc_dir="" dir
    for dir in "$PROJECT_DIR/cmake-build-debug" "$PROJECT_DIR/cmake-build-release" \
               "$PROJECT_DIR/cmake-build-relwithdebinfo" "$PROJECT_DIR/build"; do
        if [ -f "$dir/compile_commands.json" ]; then cc_dir="$dir"; break; fi
    done

    echo ""
    if [ -n "$cc_dir" ]; then
        echo "Found a configured build: $cc_dir/compile_commands.json"
        if confirm "Build the clast index now? (first run can take several minutes)"; then
            "$SCRIPT_DIR/index.sh" "$cc_dir" \
                || echo "Indexing failed — run ./$CLAST_REL/index.sh yourself once resolved."
        else
            echo "Skipped. Build it later with:  ./$CLAST_REL/index.sh"
        fi
    else
        echo "No compile_commands.json found yet — the index needs a configured build first."
        echo "Add -DCMAKE_EXPORT_COMPILE_COMMANDS=ON to your CMake options (in CLion: Settings >"
        echo "Build, Execution, Deployment > CMake), configure/build, then:  ./$CLAST_REL/index.sh"
    fi
    echo ""
    # If the project wires clast's CMake target (add_clast_index in CMakeLists.txt),
    # a normal build refreshes the index — point at that rather than only index.sh.
    if grep -q 'add_clast_index' "$PROJECT_DIR/CMakeLists.txt" 2>/dev/null; then
        echo "This project wires clast's CMake 'ast-index' target, so a normal build keeps the"
        echo "index current automatically (incremental — negligible on no-change builds). Refresh"
        echo "on demand with:  cmake --build <build-dir> --target ast-index   (or ./$CLAST_REL/index.sh)."
    else
        echo "From now on, keep the index current with ./$CLAST_REL/index.sh after notable changes"
        echo "(incremental; --force rebuilds from scratch). To refresh it as part of your build"
        echo "instead, wire clast's CMake target: include(clast/cmake/ClastIndex.cmake) +"
        echo "add_clast_index(<your-target>) in CMakeLists.txt."
    fi
}

echo ""
update_gitignore
update_claude_md
register_mcp
remove_legacy_claude_md
offer_index