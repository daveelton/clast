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

## Install

```bash
# Standalone:
pip install -e .

# As a git submodule in another project:
git submodule add <repo-url> clast
./clast/bootstrap.sh
```

## Quick start

### 1. Generate compile_commands.json (if using CMake)

```bash
cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

### 2. Index your project

```bash
# With compile_commands.json (recommended — accurate macro/include resolution):
clang-ast-mcp index /path/to/your/project \
  --compile-commands /path/to/build \
  --db /path/to/your/project/.ast-index.db

# Without compile_commands.json (falls back to -std=c++17):
clang-ast-mcp index /path/to/your/project \
  --db /path/to/your/project/.ast-index.db
```

### 3. Configure Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "clang-ast": {
      "command": "./clast/.venv/bin/python3",
      "args": ["-m", "clang_ast_mcp", "serve", "--db", ".ast-index.db"],
      "env": {
        "LIBCLANG_PATH": "/opt/homebrew/opt/llvm/lib/libclang.dylib"
      }
    }
  }
}
```

Add to your `CLAUDE.md`:

```markdown
## Code exploration

Before reading files or using grep, use the AST index tools to find code:
- `ast_search` — keyword search for symbols (use when you don't know the exact name)
- `ast_get_symbol` — get a symbol's full definition by name
- `ast_get_outline` — get a class interface or file structure (no bodies)
- `ast_get_references` — find all call sites / usages of a symbol
- `ast_get_hierarchy` — get inheritance tree for a class

Only fall back to Read/Grep if the AST tools don't return enough context.
```

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
