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

From your project root, clone the repo and add it to `.gitignore` so neither
the tool nor its index database get committed to your project:

```bash
git clone git@github.com:daveelton/clast.git
echo "clast/" >> .gitignore
./clast/bootstrap.sh
```

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

Then build the index explicitly:

```bash
cmake --build cmake-build-debug --target ast-index
```

The `ast-index` target depends on your main target, so it will build your
project first if needed. It is not part of the default `all` target — it
only runs when you ask for it.

### 3. Configure Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "clang-ast": {
      "command": "./clast/.venv/bin/python3",
      "args": ["-m", "clang_ast_mcp", "serve", "--db", "./clast/.ast-index.db"],
      "env": {
        "LIBCLANG_PATH": "/opt/homebrew/opt/llvm/lib/libclang.dylib"
      }
    }
  }
}
```

`bootstrap.sh` will offer to append clast instructions to your project's
`CLAUDE.md`. If you prefer to do it manually, copy the contents of
[CLAUDE-CLAST-ADDITION.md](CLAUDE-CLAST-ADDITION.md) into your project's
`CLAUDE.md`.

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

## Architecture

```
compile_commands.json
        │
        ▼
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  libclang │────▶│  SQLite  │────▶│   BM25   │
  │  indexer  │     │   index  │     │  search  │
  └──────────┘     └──────────┘     └──────────┘
                         │
                         ▼
                   ┌──────────┐
                   │ MCP tools │◀──── Claude Code
                   │  (stdio)  │
                   └──────────┘
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

## Xcode projects

Xcode doesn't produce `compile_commands.json` natively. Use
[Bear](https://github.com/rizsotto/Bear) to generate one by intercepting
compiler calls during a real build:

```bash
brew install bear
bear -- xcodebuild -project Foo.xcodeproj -scheme Foo build
```

Then run `./clast/index.sh` as usual — it will find the generated
`compile_commands.json` in the project root.
