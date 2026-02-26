## Code exploration — IMPORTANT

This project has a semantic C++ index (clast). You MUST use it as your
primary way to explore and understand code. Do NOT read entire files or
grep the source tree when the AST tools can answer the question directly.

**Default workflow — use in this order:**

1. `ast_search` — find symbols by keyword ("parameter smoothing", "processBlock").
   Use this first when you don't know the exact name.
2. `ast_get_symbol` — get the full definition of a symbol by name.
   Returns the complete source body, signature, and doc comment.
3. `ast_get_outline` — get a class or file's interface (signatures, no bodies).
   Use this to understand structure before diving into implementations.
4. `ast_get_references` — find all call sites / usages of a symbol.
5. `ast_get_hierarchy` — get the inheritance tree for a class.

**Only fall back to Read/Grep/Glob when:**
- You need to see non-code files (CMakeLists.txt, configs, etc.)
- You need to edit a file (read it first, then edit)
- The AST index doesn't cover the file (check with `ast_status`)

Using Read to browse .cpp/.h files wastes tokens and is slower than a
targeted AST query. Prefer multiple small AST queries over reading whole files.