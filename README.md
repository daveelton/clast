# Clang AST MCP Server

Semantic C++ code index for Claude Code. Reduces token consumption by 
delivering precise code chunks — function bodies, class outlines, call sites — 
instead of making Claude read entire files.

## How it works

1. **Index** — Clang parses your C++ source files, extracting symbols (functions, 
   classes, methods) with their signatures, doc comments, source bodies, and 
   cross-references. Stored in a local SQLite database.

2. **Query** — Claude Code calls MCP tools (`ast_search`, `ast_get_symbol`, etc.) 
   to retrieve just the code it needs. No file reading, no grep, no glob.

## Prerequisites

- **Python 3.11+**
- **libclang** (system package: `apt install libclang-dev` or `brew install llvm`)
- **python3-clang** (matching version: `apt install python3-clang-18`)
- **compile_commands.json** from CMake (recommended but not required)

## Quick start

### 0. Clone into your C++ project

From your project root, clone the repo and run `bootstrap.sh`:

```bash
git clone git@github.com:daveelton/clast.git
./clast/bootstrap.sh
```

clast is **opt-in and non-invasive** — `bootstrap.sh` only touches files that are
personal to you and never committed to the parent project. It:

- creates a self-contained venv at `clast/.venv`,
- writes the clast instructions to **`CLAUDE.local.md`** (personal, gitignored —
  not the shared `CLAUDE.md`),
- registers the `clang-ast` MCP server in Claude Code's **local scope**
  (per-project user settings, with `LIBCLANG_PATH` auto-detected), and
- adds `clast/` and `CLAUDE.local.md` to the parent project's `.gitignore`.

A teammate who never runs `bootstrap.sh` therefore sees zero clast footprint: no
MCP server entry to fail, no committed instructions referencing tools they don't
have. Re-running `bootstrap.sh` is safe and idempotent (it also upgrades the
`CLAUDE.local.md` instructions block when the version changes).

### 1. Generate compile_commands.json (if using CMake)

```bash
cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

For CLion: Settings > Build, Execution, Deployment > CMake — append
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` to the "CMake options" field, then rebuild.

### 2. Index your project

There are two ways to run the indexer: standalone or via CMake.

#### Option A: Standalone (simplest)

Run the indexer directly from the command line:

```bash
cd clast
./index.sh                              # auto-finds compile_commands.json
./index.sh /path/to/cmake-build-debug   # or specify the build dir
./index.sh --force                       # re-index everything
```

This works well when all headers exist before indexing. If your project
generates headers during the build (e.g. JUCE's `JuceHeader.h`), build first,
then run `index.sh`.

#### Option B: CMake integration

For projects with generated headers, integrate the indexer as a CMake build
target. This guarantees the index runs after the build, so all headers exist.

Add to your `CMakeLists.txt`:

```cmake
include(clast/cmake/ClastIndex.cmake)
add_clast_index(YourMainTarget)
```

The `ast-index` target is part of the default `ALL` build, so the index is
updated automatically every time you build. The indexer is incremental
(content-hash based), so no-change builds add negligible overhead.

**Important for opt-in / gitignored checkouts:** because `clast/` is not committed
to the parent project, teammates who haven't cloned it won't have
`clast/cmake/ClastIndex.cmake`, and a bare `include()` would fail their CMake
configure. Guard it so the build degrades gracefully:

```cmake
# Place this AFTER your main target is defined — add_clast_index depends on it.
# The indexer needs compile_commands.json, so ensure CMAKE_EXPORT_COMPILE_COMMANDS
# is ON. Set it *early* (before any targets are created) or configure with
# -DCMAKE_EXPORT_COMPILE_COMMANDS=ON — setting it next to the include below is too
# late, as CMake fixes the per-target export property at target-creation time.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/clast/cmake/ClastIndex.cmake")
    include(clast/cmake/ClastIndex.cmake)
    add_clast_index(YourMainTarget)
endif()
```

With clast absent the block is a no-op and the build is unaffected; with clast
present the index refreshes on every build. (`add_clast_index` is itself
defensive — if the venv is missing it skips creating the target rather than
erroring.)

### 3. Configure Claude Code

`bootstrap.sh` already did this — it registered the `clang-ast` MCP server in
Claude Code's **local scope** (not a committed `.mcp.json`) and wrote the clast
instructions to `CLAUDE.local.md`. Nothing further is needed; restart Claude Code
to pick up the new MCP server.

If `bootstrap.sh` couldn't find the `claude` CLI or auto-detect libclang, it
prints the exact command to run. Manually, that is:

```bash
claude mcp add --scope local clang-ast \
  -e LIBCLANG_PATH=/opt/homebrew/opt/llvm/lib/libclang.dylib \
  -- bash -c "./clast/.venv/bin/python3 -m clang_ast_mcp serve --db ./clast/.ast-index.db 2>>./clast/mcp.log"
```

Use **relative** paths in the serve command (resolved at launch against the
session's working directory), not absolute ones — see [Git worktrees](#git-worktrees)
for why. Run this from the project root so `./clast/...` resolves correctly.

Logs are appended to `clast/mcp.log` — use `tail -f clast/mcp.log` to watch tool
calls, response sizes, and timing in real time.

> Prefer a committed, team-shared setup instead of opt-in? Add the server to a
> project-scoped `.mcp.json` and copy [CLAUDE-CLAST-ADDITION.md](CLAUDE-CLAST-ADDITION.md)
> into the shared `CLAUDE.md`. Note this commits a clang-ast entry that fails for
> any teammate who hasn't run `bootstrap.sh`, and hardcodes `LIBCLANG_PATH`.

## MCP Tools

| Tool | Purpose | When to use |
|------|---------|-------------|
| `ast_search` | Keyword/natural language search | "How does parameter smoothing work?" |
| `ast_get_symbol` | Get full definition by name | "Show me MyPlugin::processBlock" |
| `ast_get_outline` | Class/file outline (no bodies) | "What's the interface of MyPlugin?" |
| `ast_get_references` | Find call sites | "What calls parameterChanged?" |
| `ast_get_hierarchy` | Inheritance tree | "What derives from AudioProcessor?" |
| `ast_index` | Re-index project | After changing files |
| `ast_status` | Index stats | Check if index is loaded |

## Output format

By default, tool responses use a **compact** plain-text format that renders
native C++ syntax instead of JSON field names, saving ~3,000-5,000 tokens per
session. See [docs/compact-response-format.md](docs/compact-response-format.md)
for the full specification and rationale.

To switch to JSON output, use the CLI flag:

```bash
python3 -m clang_ast_mcp serve --db .ast-index.db --format json
```

Or the environment variable:

```bash
AST_OUTPUT_FORMAT=json python3 -m clang_ast_mcp serve --db .ast-index.db
```

In `.mcp.json`:

```json
{
  "mcpServers": {
    "clang-ast": {
      "command": "bash",
      "args": ["-c", "./clast/.venv/bin/python3 -m clang_ast_mcp serve --db ./clast/.ast-index.db 2>>./clast/mcp.log"],
      "env": {
        "AST_OUTPUT_FORMAT": "json",
        "LIBCLANG_PATH": "/opt/homebrew/opt/llvm/lib/libclang.dylib"
      }
    }
  }
}
```

The format is server-wide — restart with a different setting to switch. This
makes A/B comparison straightforward: run a full session in each mode and compare
token usage from Claude Code's reporting.

## Architecture

```
compile_commands.json
        │
        ▼
  ┌──────────┐     ┌───────────┐     ┌──────────┐
  │ libclang │────▶│  SQLite   │────▶│   BM25   │
  │ indexer  │     │   index   │     │  search  │
  └──────────┘     └───────────┘     └──────────┘
                         │
                         ▼
                   ┌───────────┐
                   │ MCP tools │◀──── Claude Code
                   │  (stdio)  │
                   └───────────┘
```

- **Indexer** uses `libclang` to parse each translation unit, extracting symbols 
  with USRs (Unified Symbol Resolution IDs) for precise cross-referencing
- **Storage** is SQLite with indexes on symbol names, USRs, and file paths
- **Search** uses BM25 keyword ranking over symbol names, signatures, and doc comments
- **MCP server** uses FastMCP with stdio transport (local to Claude Code)

## Incremental re-indexing

The indexer tracks file content hashes. Running `ast_index` again only re-parses 
files that have changed. Use `--force` to re-index everything.

## Indexing third-party headers

For JUCE or similar large frameworks, index only the public headers:

```bash
clang-ast-mcp index /path/to/JUCE/modules \
  --db /path/to/your/project/.ast-index.db
```

This gives Claude access to JUCE class outlines and API signatures without
indexing implementation files.

## Advanced topics

### Manual CMake index target

If you prefer to run the indexer on demand rather than on every build, pass
`EXCLUDE_FROM_ALL`:

```cmake
include(clast/cmake/ClastIndex.cmake)
add_clast_index(YourMainTarget EXCLUDE_FROM_ALL)
```

Then build the index explicitly:

```bash
cmake --build cmake-build-debug --target ast-index
```

The `ast-index` target depends on your main target, so it will build your
project first if needed.

### Git worktrees

If you use multiple git worktrees of the same repo (e.g. a `develop` checkout
and a maintenance-branch worktree), they share one underlying repository. The
`clang-ast` local-scope registration may end up shared across them — observed
behaviour is that the `claude` CLI writes under the worktree's own path key, but
Claude Code can canonicalise worktrees onto a single key when it reads config.
Either way, `bootstrap.sh` handles this by registering the serve command with
**relative** paths:

```
bash -c "./clast/.venv/bin/python3 -m clang_ast_mcp serve --db ./clast/.ast-index.db 2>>./clast/mcp.log"
```

Relative paths are resolved at launch against each session's working directory,
so the *same* entry gives each worktree its own `clast/.venv` and
`.ast-index.db`. An **absolute** `--db` path would instead pin every worktree to
whichever one last ran `bootstrap.sh` — so a session in another worktree would
silently load the wrong index. This is why the registered command (and the
manual fallback above) must stay relative. Each worktree still needs its own
`bootstrap.sh` run to create its `clast/.venv` and index.

Two caveats to this approach:

- **Launch directory matters.** Relative resolution assumes Claude Code launches
  the MCP server with its working directory at the worktree root. Starting a
  session from a *subdirectory* would make `./clast/...` resolve against that
  subdir and fail. If you need to tolerate that, register a self-rooting command
  instead: `bash -c 'cd "$(git rev-parse --show-toplevel)" && exec ./clast/.venv/bin/python3 -m clang_ast_mcp serve --db ./clast/.ast-index.db 2>>./clast/mcp.log'`.
- **It depends on Claude Code internals** (the per-session launch cwd, and how
  worktrees are keyed) that aren't a documented contract. Re-confirm after major
  Claude Code upgrades — a regression here is quiet (wrong index, not an error).

### Xcode projects

Xcode doesn't produce `compile_commands.json` natively. You may be able to use
[Bear](https://github.com/rizsotto/Bear) to generate one by intercepting
compiler calls during a real build:

```bash
brew install bear
bear -- xcodebuild -project Foo.xcodeproj -scheme Foo build
```

Then run `./clast/index.sh` as usual — it will find the generated
`compile_commands.json` in the project root.

## LLM Generated Code Caveat Emptor
The content of this project contains largely LLM-generated code with all the benefits and limitations that entails.
