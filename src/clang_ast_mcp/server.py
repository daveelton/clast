"""Clang AST MCP Server.

Exposes C++ codebase index as MCP tools for Claude Code. Provides
symbol lookup, class outlines, reference finding, type hierarchy,
and keyword search — all backed by a Clang-parsed AST stored in SQLite.

Usage:
    # Index a project (run once, then incrementally):
    python -m clang_ast_mcp.server index /path/to/project --compile-commands /path/to/build

    # Run as MCP server (stdio transport for Claude Code):
    python -m clang_ast_mcp.server serve --db /path/to/project/.ast-index.db
"""

import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

from .db import ASTDatabase
from .search import SymbolSearch

log = logging.getLogger(__name__)

# ── Global state (initialized in lifespan) ──────────────────────────

_db: ASTDatabase | None = None
_search: SymbolSearch | None = None
_db_path: str = ""
_db_inode: int = 0
_db_mtime: float = 0.0
_last_check: float = 0.0
_CHECK_INTERVAL = 5.0  # seconds between freshness checks


def _get_db_stat() -> tuple[int, float]:
    """Get inode and mtime of the DB file."""
    try:
        st = os.stat(_db_path)
        return st.st_ino, st.st_mtime
    except OSError:
        return 0, 0.0


def _ensure_db_fresh():
    """Reopen DB and rebuild search index if the DB file has changed."""
    global _db, _search, _db_inode, _db_mtime, _last_check

    now = time.monotonic()
    if now - _last_check < _CHECK_INTERVAL:
        return
    _last_check = now

    inode, mtime = _get_db_stat()
    if inode == _db_inode and mtime == _db_mtime:
        return

    log.info("DB changed (inode %s→%s, mtime %s→%s), reopening",
             _db_inode, inode, _db_mtime, mtime)
    try:
        if _db:
            _db.close()
    except Exception:
        pass

    _db = ASTDatabase(_db_path)
    _search = SymbolSearch(_db)
    _search.build_index()
    _db_inode = inode
    _db_mtime = mtime
    stats = _db.stats()
    log.info("Reloaded: %d symbols, %d references, %d files",
             stats["symbols"], stats["references"], stats["files"])


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Initialize database and search index on startup."""
    global _db, _search, _db_path, _db_inode, _db_mtime

    _db_path = os.environ.get("AST_DB_PATH", ".ast-index.db")
    log.info("Opening AST database: %s", _db_path)

    _db = ASTDatabase(_db_path)
    _search = SymbolSearch(_db)
    _search.build_index()
    _db_inode, _db_mtime = _get_db_stat()

    stats = _db.stats()
    log.info("AST index loaded: %d symbols, %d references, %d files",
             stats["symbols"], stats["references"], stats["files"])

    yield {"db": _db, "search": _search}

    _db.close()


mcp = FastMCP("clang_ast_mcp", lifespan=app_lifespan)


# ── Input models ────────────────────────────────────────────────────


class SearchInput(BaseModel):
    """Input for keyword/semantic search across symbols."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Keyword or natural language query. Searches symbol names, "
                    "signatures, doc comments, and parameter types. "
                    "Examples: 'processBlock audio callback', 'parameter smoothing', "
                    "'preset save restore state'",
        min_length=1,
        max_length=500,
    )
    limit: int = Field(
        default=10,
        description="Maximum number of results to return",
        ge=1, le=50,
    )
    kinds: Optional[list[str]] = Field(
        default=None,
        description="Filter by symbol kind: 'function', 'method', 'class', 'struct', "
                    "'field', 'enum', 'variable', 'namespace', 'typedef'. "
                    "Pass null for all kinds.",
    )


class SymbolInput(BaseModel):
    """Input for looking up a symbol by name."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description="Symbol name to look up. Can be unqualified ('processBlock'), "
                    "partially qualified ('MyPlugin::processBlock'), or fully qualified "
                    "('ns::MyPlugin::processBlock'). Case-insensitive partial matching.",
        min_length=1,
        max_length=300,
    )
    include_body: bool = Field(
        default=True,
        description="Include the full source body in the response. Set to false for "
                    "just the signature and metadata.",
    )


class OutlineInput(BaseModel):
    """Input for getting a class or file outline."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description="Class name (e.g. 'MyPlugin') or file path (e.g. 'src/PluginProcessor.h'). "
                    "For classes, returns member signatures without bodies. "
                    "For files, returns all top-level declarations.",
        min_length=1,
        max_length=500,
    )


class ReferencesInput(BaseModel):
    """Input for finding references/call sites."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description="Qualified symbol name to find references for. "
                    "Example: 'MyPlugin::parameterChanged'",
        min_length=1,
        max_length=300,
    )
    limit: int = Field(
        default=30,
        description="Maximum references to return",
        ge=1, le=100,
    )
    context_lines: int = Field(
        default=3,
        description="Number of lines of surrounding context to include above and below "
                    "each reference (like grep -C). 0 = single-line context only.",
        ge=0, le=15,
    )


class HierarchyInput(BaseModel):
    """Input for getting a type hierarchy."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description="Class name to get the inheritance hierarchy for. "
                    "Returns base classes (upward) and known derived classes (downward).",
        min_length=1,
        max_length=300,
    )


class IndexInput(BaseModel):
    """Input for triggering (re-)indexing."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ...,
        description="Directory to index, or path to compile_commands.json. "
                    "If a directory, recursively indexes all C++ files. "
                    "If compile_commands.json, indexes all listed translation units.",
        min_length=1,
    )
    compile_commands_dir: Optional[str] = Field(
        default=None,
        description="Directory containing compile_commands.json for accurate parsing. "
                    "Usually the CMake build directory.",
    )
    force: bool = Field(
        default=False,
        description="Force re-index even if files haven't changed.",
    )


# ── Helper formatting ───────────────────────────────────────────────


def _format_symbol(sym, include_body: bool = True) -> dict:
    """Format a Symbol into the response shape from the requirements."""
    result = {
        "symbol": sym.qualified_name,
        "kind": sym.kind,
        "signature": sym.signature,
        "file": sym.file,
        "lines": [sym.line_start, sym.line_end],
    }
    if sym.doc:
        result["doc"] = sym.doc
    if sym.parent_name:
        result["parent"] = sym.parent_name
    if sym.return_type:
        result["return_type"] = sym.return_type
    if sym.parameters:
        result["parameters"] = sym.parameters
    if sym.attributes:
        result["attributes"] = sym.attributes
    if sym.base_names:
        result["bases"] = sym.base_names
    if include_body and sym.body:
        result["body"] = sym.body
    return result


# ── Tools ───────────────────────────────────────────────────────────


@mcp.tool(
    name="ast_search",
    annotations={
        "title": "Search C++ Symbols",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_search(params: SearchInput) -> str:
    """Search for C++ symbols by keyword or natural language query.

    Searches across symbol names, signatures, doc comments, parameter types,
    and parent scope names. Returns ranked results with signatures and file locations.

    Use this for exploratory queries like "parameter smoothing audio callback" or
    "preset save restore" when you don't know the exact symbol name.

    Args:
        params (SearchInput): Search parameters containing:
            - query (str): Keywords or natural language description
            - limit (int): Max results (default 10)
            - kinds (list[str] | None): Filter by symbol kind

    Returns:
        str: JSON with ranked search results including symbol, signature, file, lines, snippet, score
    """
    _ensure_db_fresh()
    results = _search.search(params.query, limit=params.limit, kinds=params.kinds)

    if not results:
        return json.dumps({"query": params.query, "results": [], "message": "No matching symbols found."})

    formatted = []
    for r in results:
        entry = {
            "symbol": r.symbol.qualified_name,
            "kind": r.symbol.kind,
            "signature": r.symbol.signature,
            "file": r.symbol.file,
            "lines": [r.symbol.line_start, r.symbol.line_end],
            "score": round(r.score, 3),
        }
        if r.symbol.doc:
            entry["snippet"] = r.symbol.doc[:200]
        formatted.append(entry)

    return json.dumps({"query": params.query, "results": formatted}, indent=2)


@mcp.tool(
    name="ast_get_symbol",
    annotations={
        "title": "Get Symbol Definition",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_get_symbol(params: SymbolInput) -> str:
    """Get the full definition of a C++ symbol by name.

    Returns the complete definition chunk: signature, doc comment, file path,
    line range, parent scope, and optionally the source body. Resolves the
    symbol precisely — no false positives from text matching.

    Use this when you know the symbol name and need its implementation.

    Args:
        params (SymbolInput): Lookup parameters containing:
            - name (str): Symbol name (qualified or unqualified)
            - include_body (bool): Whether to include source body (default True)

    Returns:
        str: JSON with symbol definition including signature, file, lines, body, doc
    """
    _ensure_db_fresh()
    matches = _db.find_symbols_by_name(params.name)

    if not matches:
        return json.dumps({
            "error": f"Symbol '{params.name}' not found.",
            "suggestion": "Try ast_search with keywords to discover the correct name.",
        })

    if len(matches) == 1:
        return json.dumps(_format_symbol(matches[0], include_body=params.include_body), indent=2)

    # Multiple matches — return all but truncate bodies
    results = []
    for sym in matches[:10]:
        results.append(_format_symbol(sym, include_body=params.include_body))

    return json.dumps({
        "query": params.name,
        "matches": len(matches),
        "results": results,
        "note": f"Found {len(matches)} symbols matching '{params.name}'. "
                "Use a more qualified name to narrow down.",
    }, indent=2)


@mcp.tool(
    name="ast_get_outline",
    annotations={
        "title": "Get Class or File Outline",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_get_outline(params: OutlineInput) -> str:
    """Get the outline (member signatures, no bodies) of a class or file.

    For classes: returns base classes, all member signatures, and doc comments.
    For files: returns all top-level declarations with signatures.

    Use this to understand a class interface or file structure without reading
    implementation details.

    Args:
        params (OutlineInput): Contains:
            - name (str): Class name or file path

    Returns:
        str: JSON with class/file outline including member signatures
    """
    _ensure_db_fresh()
    # Try as a class first
    matches = _db.find_symbols_by_name(params.name)
    class_matches = [m for m in matches if m.kind in ("class", "struct", "class_template")]

    if class_matches:
        cls = class_matches[0]
        members = _db.get_class_members(cls.usr)
        member_sigs = []
        for m in members:
            entry = {"kind": m.kind, "signature": m.signature}
            if m.doc:
                entry["doc"] = m.doc
            if m.attributes:
                entry["attributes"] = m.attributes
            member_sigs.append(entry)

        result = {
            "name": cls.qualified_name,
            "kind": cls.kind,
            "file": cls.file,
            "lines": [cls.line_start, cls.line_end],
            "members": member_sigs,
        }
        if cls.base_names:
            result["bases"] = cls.base_names
        if cls.doc:
            result["doc"] = cls.doc
        return json.dumps(result, indent=2)

    # Try as a file path
    name = params.name
    file_symbols = _db.get_file_symbols(name)
    if not file_symbols:
        # Try matching just the filename
        all_syms = _db.get_all_symbols()
        file_symbols = [s for s in all_syms if s.file.endswith(name)]

    if file_symbols:
        # Return top-level symbols only (no parent or parent is namespace)
        top_level = []
        for s in file_symbols:
            if not s.parent_usr or s.kind == "namespace":
                entry = {
                    "kind": s.kind,
                    "name": s.qualified_name,
                    "signature": s.signature,
                    "lines": [s.line_start, s.line_end],
                }
                if s.doc:
                    entry["doc"] = s.doc
                top_level.append(entry)

        return json.dumps({
            "file": name,
            "declarations": top_level,
        }, indent=2)

    return json.dumps({
        "error": f"No class or file matching '{params.name}' found.",
        "suggestion": "Try ast_search to discover the correct name.",
    })


@mcp.tool(
    name="ast_get_references",
    annotations={
        "title": "Find References / Call Sites",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_get_references(params: ReferencesInput) -> str:
    """Find all references and call sites for a symbol.

    Returns a list of locations where the symbol is called or used, each with
    the enclosing function name, file path, line number, and context.
    Uses Clang's USR resolution for precision — no false positives from string matching.

    Set context_lines > 0 to include surrounding source lines at each call site
    (like grep -C), so you can see how the symbol is used without reading the file.

    Args:
        params (ReferencesInput): Contains:
            - name (str): Qualified symbol name
            - limit (int): Max references to return
            - context_lines (int): Lines of surrounding context (0 = single line)

    Returns:
        str: JSON with list of reference sites including caller, file, line, context
    """
    _ensure_db_fresh()
    matches = _db.find_symbols_by_name(params.name)

    if not matches:
        return json.dumps({
            "error": f"Symbol '{params.name}' not found.",
            "suggestion": "Try ast_search to discover the correct name.",
        })

    # Use the best match
    sym = matches[0]
    refs = _db.get_references_to(sym.usr)

    # Deduplicate by (file, line)
    seen = set()
    unique_refs = []
    for ref in refs:
        key = (ref["file"], ref["line"])
        if key not in seen:
            seen.add(key)
            unique_refs.append(ref)

    result_refs = unique_refs[:params.limit]

    # Expand context if requested
    if params.context_lines > 0:
        file_cache: dict[str, list[str]] = {}
        for ref in result_refs:
            fpath = ref["file"]
            if fpath not in file_cache:
                try:
                    file_cache[fpath] = Path(fpath).read_text(errors="replace").splitlines()
                except Exception:
                    file_cache[fpath] = []
            lines = file_cache[fpath]
            if lines:
                line_idx = ref["line"] - 1
                start = max(0, line_idx - params.context_lines)
                end = min(len(lines), line_idx + params.context_lines + 1)
                numbered = [
                    f"{'>' if i == line_idx else ' '} {i + 1:4d} | {lines[i]}"
                    for i in range(start, end)
                ]
                ref["context"] = "\n".join(numbered)

    return json.dumps({
        "symbol": sym.qualified_name,
        "total_references": len(unique_refs),
        "references": result_refs,
    }, indent=2)


@mcp.tool(
    name="ast_get_hierarchy",
    annotations={
        "title": "Get Type Hierarchy",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_get_hierarchy(params: HierarchyInput) -> str:
    """Get the inheritance hierarchy for a class.

    Returns direct base classes (upward) and known derived classes (downward),
    each with their file location.

    Args:
        params (HierarchyInput): Contains:
            - name (str): Class name

    Returns:
        str: JSON with bases and derived classes, each with name, file, line
    """
    _ensure_db_fresh()
    matches = _db.find_symbols_by_name(params.name)
    class_matches = [m for m in matches if m.kind in ("class", "struct", "class_template")]

    if not class_matches:
        return json.dumps({
            "error": f"Class '{params.name}' not found.",
            "suggestion": "Try ast_search to discover the correct name.",
        })

    cls = class_matches[0]

    # Bases (upward)
    bases = []
    for usr, name in zip(cls.bases, cls.base_names):
        entry = {"name": name}
        if usr:
            base_sym = _db.get_symbol_by_usr(usr)
            if base_sym:
                entry["file"] = base_sym.file
                entry["line"] = base_sym.line_start
        bases.append(entry)

    # Derived classes (downward)
    derived_syms = _db.find_derived_classes(cls.usr)
    derived = [
        {"name": d.qualified_name, "file": d.file, "line": d.line_start}
        for d in derived_syms
    ]

    return json.dumps({
        "symbol": cls.qualified_name,
        "file": cls.file,
        "bases": bases,
        "derived": derived,
    }, indent=2)


@mcp.tool(
    name="ast_index",
    annotations={
        "title": "Index C++ Project",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_index(params: IndexInput) -> str:
    """Index or re-index a C++ project directory.

    Parses all C++ source files using Clang, extracting symbols, signatures,
    doc comments, and cross-references. Uses compile_commands.json when available
    for accurate parsing. Only re-parses files that have changed since last index.

    Run this once on your project, then incrementally as files change.

    Args:
        params (IndexInput): Contains:
            - path (str): Directory or compile_commands.json path
            - compile_commands_dir (str | None): Build dir with compile_commands.json
            - force (bool): Force re-index unchanged files

    Returns:
        str: JSON with indexing stats (files indexed, symbols found, etc.)
    """
    from .indexer import Indexer

    cc_dir = params.compile_commands_dir
    target = Path(params.path)

    # If path points to compile_commands.json, use its directory
    if target.name == "compile_commands.json":
        cc_dir = str(target.parent)
        target = target.parent

    indexer = Indexer(_db, compile_commands_dir=cc_dir, project_root=str(target))

    if cc_dir and (Path(cc_dir) / "compile_commands.json").exists():
        result = indexer.index_from_compile_commands(force=params.force)
    else:
        result = indexer.index_directory(str(target), force=params.force)

    # Rebuild search index after indexing
    _search.build_index()

    # Add current DB stats
    result["db_stats"] = _db.stats()
    return json.dumps(result, indent=2)


@mcp.tool(
    name="ast_status",
    annotations={
        "title": "Index Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def ast_status() -> str:
    """Get the current status of the AST index.

    Returns the number of indexed symbols, references, and files.

    Returns:
        str: JSON with index statistics
    """
    stats = _db.stats()
    return json.dumps(stats, indent=2)


# ── CLI entry point ─────────────────────────────────────────────────


def main():
    """CLI entry point for indexing and serving."""
    import argparse

    parser = argparse.ArgumentParser(description="Clang AST MCP Server")
    sub = parser.add_subparsers(dest="command")

    # serve command
    serve_cmd = sub.add_parser("serve", help="Run as MCP server (stdio)")
    serve_cmd.add_argument("--db", default=".ast-index.db", help="Path to SQLite database")

    # index command
    idx_cmd = sub.add_parser("index", help="Index a C++ project")
    idx_cmd.add_argument("path", help="Directory to index")
    idx_cmd.add_argument("--compile-commands", "-c", help="Directory containing compile_commands.json")
    idx_cmd.add_argument("--db", default=".ast-index.db", help="Path to SQLite database")
    idx_cmd.add_argument("--force", "-f", action="store_true", help="Force re-index")

    args = parser.parse_args()

    # Force unbuffered stderr so progress appears immediately in CLion/CMake
    sys.stderr = open(sys.stderr.fileno(), "w", buffering=1, closefd=False)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # MCP stdio: logs must go to stderr
    )

    if args.command == "index":
        # Direct indexing without MCP server
        db = ASTDatabase(args.db)
        from .indexer import Indexer
        indexer = Indexer(db, compile_commands_dir=args.compile_commands,
                         project_root=args.path)

        if args.compile_commands and (Path(args.compile_commands) / "compile_commands.json").exists():
            result = indexer.index_from_compile_commands(force=args.force)
        else:
            result = indexer.index_directory(args.path, force=args.force)

        result["db_stats"] = db.stats()
        print(json.dumps(result, indent=2))
        db.close()

    elif args.command == "serve" or args.command is None:
        import os
        db_path = args.db if hasattr(args, "db") else ".ast-index.db"
        os.environ["AST_DB_PATH"] = db_path
        mcp.run()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
