# Future features

A list of potential future features.

## Include dependency tracking

**Status:** Proposed

### Problem

When refactoring headers (e.g. removing a transitive include), Claude Code
falls back to multi-file grep loops like:

```bash
# 1. List includes per file
for file in $FILES; do grep "#include" "$file" | head -15; done

# 2. Check if specific headers are directly included
for file in $FILES; do grep "target_header.h" "$file"; done

# 3. Find which files use symbols defined in that header
for file in $FILES; do grep -E "SymbolA|SymbolB|SymbolC" "$file"; done
```

Step 3 is already handled by `ast_get_references` — one call per symbol,
precise USR-based matching, no false positives. Steps 1 and 2 have no AST
equivalent today.

### Proposed solution

Clang exposes `INCLUSION_DIRECTIVE` cursors during AST traversal. We can
extract these and store them.

**Indexer:** Record `#include` directives per translation unit.

**DB:** New `includes` table:
| Column | Type | Description |
|--------|------|-------------|
| file | text | Source file containing the `#include` |
| included_header | text | Resolved path of the included header |
| line | int | Line number of the directive |
| is_system | bool | Whether it uses `<>` vs `""` |

**MCP tools:**
- `ast_get_includes(file)` — list all headers included by a file
- `ast_get_includers(header)` — list all files that include a given header
  (reverse lookup)
- `ast_trace_include(from, to)` — find the transitive include chain between
  two headers (see below)

### Cost/benefit analysis

**TODO:** Weigh the expected benefit of these tools against the context cost of
adding MCP endpoints:

- **Token savings:** Estimate typical token cost of the grep-loop pattern
  (reading N files × M lines each) versus a single `ast_get_includes` or
  `ast_get_includers` call returning structured JSON. Consider that the grep
  pattern often requires 2–3 sequential rounds (list includes → check specific
  headers → check symbol usage) while the AST tools could answer in 1–2 calls.

- **Context cost:** Each MCP tool adds to the tool description payload that
  Claude receives at the start of every conversation. Measure the token
  overhead of the new tool schemas + descriptions and compare against the
  per-query savings above. Consider whether `ast_get_includes` and
  `ast_trace_include` could be combined into one tool with an optional
  `trace_to` parameter to reduce the number of endpoints.

- **Frequency:** How often does include-dependency analysis come up during
  typical C++ refactoring sessions? If it's rare, the constant context cost
  may outweigh the occasional savings.

---

## Transitive include chain tracing

**Status:** Proposed
**Depends on:** Include dependency tracking (above)

### Problem

When a refactoring breaks a transitive include, Claude Code writes ad-hoc
Python scripts to trace the include graph. For example, to answer "how does
`header_a.h` transitively include `header_b.h`?", Claude generates a ~30-line
DFS script that:

1. Parses `#include "..."` directives with regex
2. Recursively resolves filenames by searching `Source/` and `lib/`
3. Walks the graph depth-first to find the path

This is a common need during header refactoring — understanding *why* removing
an include breaks something requires knowing the full transitive chain.

### Why `ast_get_includes` alone isn't enough

With only `ast_get_includes(file)`, Claude would need to manually recurse:
call the tool on header A, then on each of its includes, then on each of
theirs, etc. For a project with deep include trees this could be dozens of
sequential MCP round trips — slower and more token-expensive than the script.

### Proposed solution

Add `ast_trace_include(from, to)` — a server-side BFS/DFS over the include
graph that returns the shortest chain:

```json
{
  "from": "header_a.h",
  "to": "header_b.h",
  "chain": ["header_a.h", "intermediate.h", "types.h", "header_b.h"]
}
```

The ad-hoc scripts Claude generates produce output like:

```
=== storage_manager.cpp -> utilities.h ===
storage_manager.cpp -> doohicky.h -> thingy.h -> utilities.h

=== race_track.cpp -> utilities.h ===
race_track.cpp -> track_model.h -> doofer_output.h -> utilities.h
```

Claude then reasons about which intermediate header was providing the
transitive dependency. The tool output should match this structure closely —
a flat chain showing each hop — so that Claude can immediately reason about
which link broke.

This replaces the ad-hoc script with a single tool call. The graph traversal
happens server-side using the indexed include data, so it's fast and costs
minimal tokens.

### Design considerations

- Could be a parameter on `ast_get_includes` (e.g. `trace_to`) rather than a
  separate tool, reducing the context cost of an additional endpoint
- Should handle cycles gracefully (headers can have mutual includes via guards)
- Should report "no path found" clearly if the headers are unrelated
- Consider a `max_depth` parameter to bound the search

---

## Transitive dependency detection

**Status:** Proposed
**Depends on:** Include dependency tracking

### Problem

During include hygiene refactoring, Claude Code writes scripts to find files
that use symbols from a header without directly including it — i.e. they
depend on a transitive include that could break if an intermediate header
changes.

A typical script:

1. Takes a list of symbols exported by a header (e.g. `utilities.h`)
2. Walks all `.cpp`/`.h` files checking for symbol usage (string matching)
3. Filters out files that already have a direct `#include "utilities.h"`
4. Reports the remainder as transitive dependents

### Could existing + proposed tools solve this?

Partially. With include tracking in place, Claude could:

1. `ast_get_references(symbol)` for each symbol → files that use it
2. `ast_get_includers("utilities.h")` → files that directly include it
3. Diff the two sets

But this requires N+1 tool calls (one per symbol + one for includers) and
manual set-diffing by Claude. The script does it in one pass.

### Proposed solution

`ast_find_transitive_deps(header)` — given a header file, return all source
files that use symbols defined in it but don't directly include it:

```json
{
  "header": "utilities.h",
  "transitive_dependents": [
    {
      "file": "storage_manager.cpp",
      "symbols_used": ["initEngine", "clampValue"],
      "included_via": "foo_artefact.h"
    }
  ]
}
```

This combines the symbol index (already in the DB) with include tracking
(proposed above) into a single server-side query.

### Design considerations

- This is a compound query over two data sources (symbols + includes). It may
  be better as a convenience layer on top of the lower-level tools rather than
  a core primitive
- The `included_via` field requires include chain tracing — ties into
  `ast_trace_include`
- Could be expensive on large codebases; may need a `limit` parameter or
  restriction to project-local symbols only
- **Cost/benefit:** This replaces a ~20-line script + full source tree walk.
  But it's a relatively rare operation (include hygiene cleanup). Weigh
  against the context cost of yet another endpoint. Consider whether clear
  CLAUDE.md instructions to combine `ast_get_references` + `ast_get_includers`
  would be sufficient without a dedicated tool

---

## Alternative: raw SQL query tool

**Status:** Under consideration

### Motivation

Each new query pattern (include tracing, transitive dependency detection, etc.)
currently requires a new MCP endpoint — adding code, context cost, and
maintenance. Meanwhile, Claude is competent at writing SQL. Exposing the schema
and a read-only query tool would let Claude handle ad-hoc analysis without
new endpoints.

Other MCPs already follow this pattern — notably `@anthropic/mcp-server-sqlite`
which exposes schema inspection and arbitrary SQL queries.

### Proposed design

A single `ast_query(sql)` tool that:
1. Accepts a read-only SQL string
2. Runs it against the index database
3. Returns the result rows as JSON

Combined with an `ast_schema()` tool (or a schema summary in the tool
description) so Claude knows the table structure.

The existing high-level tools (`ast_search`, `ast_get_symbol`, `ast_get_outline`,
`ast_get_references`, `ast_get_hierarchy`) would remain — they provide curated,
token-efficient output for common operations. `ast_query` is an escape hatch
for everything else.

### Enforcing read-only access

The index DB should only be modified by the indexer. SQLite provides two
mechanisms to enforce this at query time:

**Option A — `PRAGMA query_only`:**
```python
conn = sqlite3.connect(db_path)
conn.execute("PRAGMA query_only = ON")
```
Blocks all writes (`INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`) at the
statement level. Simple to implement; set it once on the connection used by
`ast_query`.

**Option B — URI read-only mode:**
```python
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```
Opens the database file in read-only mode at the OS level. Even `PRAGMA`
statements that would modify state are blocked. Slightly stronger guarantee.

**Recommendation:** Use both. Open a separate read-only connection (URI mode)
for `ast_query`, and set `PRAGMA query_only = ON` as a belt-and-braces
measure. The main read-write connection used by the indexer remains separate.

### Tradeoffs vs purpose-built tools

| | Purpose-built tools | `ast_query` |
|---|---|---|
| Context cost | Grows per endpoint | Fixed (one tool + schema) |
| New query patterns | Require code changes | Claude handles ad-hoc |
| Output quality | Structured, summarised | Raw rows, potentially verbose |
| Token efficiency per call | Better (curated) | Worse (full result sets) |
| Schema changes | Internal detail | Become an API contract |

### Design considerations

- Adding `ast_query` may make some of the proposed include-tracking endpoints
  unnecessary — Claude could write recursive CTEs to traverse the include
  graph directly
- Should include a `LIMIT` cap (e.g. 500 rows) to prevent runaway queries
  from flooding the context
- The schema description adds constant context cost; keep it concise
- Consider whether `ast_schema()` should be a separate tool or just embedded
  in the `ast_query` tool description
- May want to log queries for debugging / understanding what patterns Claude
  reaches for, which could inform which purpose-built tools are worth adding

---

*Add new feature proposals below this line.*