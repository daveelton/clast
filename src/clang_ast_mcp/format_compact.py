"""Compact plain-text formatter for MCP tool responses.

Reconstructs native C++ declaration syntax from stored Symbol fields,
producing output that reads like source code rather than JSON. This saves
~3,000-5,000 tokens per session and matches the format LLMs are trained on.

See docs/compact-response-format.md for the full specification.
"""

from .db import Symbol


# ── Core declaration builder ─────────────────────────────────────────


def build_declaration(sym: Symbol, include_param_names: bool = True) -> str:
    """Reconstruct a C++ declaration line from Symbol fields.

    For functions/methods: ``void Class::method(int x, float y) override``
    For classes: ``class Foo : public Base``
    For fields: ``int MyClass::count``
    For enums/typedefs: uses stored signature verbatim.
    """
    kind = sym.kind

    if kind in ("constructor", "destructor"):
        params = _format_params(sym.parameters, include_param_names)
        return f"{sym.qualified_name}({params})"

    if kind in ("function", "method"):
        params = _format_params(sym.parameters, include_param_names)
        parts = []
        if sym.return_type:
            parts.append(sym.return_type)
        parts.append(f"{sym.qualified_name}({params})")
        if sym.attributes:
            parts.append(" ".join(sym.attributes))
        return " ".join(parts)

    if kind in ("class", "struct", "class_template"):
        prefix = "struct" if kind == "struct" else "class"
        if sym.base_names:
            bases = ", ".join(sym.base_names)
            return f"{prefix} {sym.qualified_name} : {bases}"
        return f"{prefix} {sym.qualified_name}"

    if kind == "enum":
        if sym.signature:
            return sym.signature
        return f"enum {sym.qualified_name}"

    if kind == "namespace":
        return f"namespace {sym.qualified_name}"

    if kind == "typedef":
        return sym.signature or f"typedef {sym.qualified_name}"

    # field, variable, or anything else — use signature if available
    if sym.signature:
        return sym.signature
    if sym.return_type:
        return f"{sym.return_type} {sym.qualified_name}"
    return sym.qualified_name


def _format_params(parameters: list[dict], include_names: bool) -> str:
    """Format a parameter list as ``type name, type name, ...``."""
    if not parameters:
        return ""
    parts = []
    for p in parameters:
        ptype = p.get("type", "")
        pname = p.get("name", "")
        if include_names and pname:
            parts.append(f"{ptype} {pname}")
        else:
            parts.append(ptype)
    return ", ".join(parts)


# ── Location header helpers ──────────────────────────────────────────


def _location(sym: Symbol, include_size: bool = False) -> str:
    """Format ``file:line`` or ``file:line  (N lines)``."""
    loc = f"{sym.file}:{sym.line_start}"
    if include_size and sym.line_end > sym.line_start:
        loc += f"  ({sym.line_end - sym.line_start + 1} lines)"
    return loc


def _doc_lines(sym: Symbol) -> str:
    """Return doc comment lines or empty string."""
    if sym.doc:
        return sym.doc + "\n"
    return ""


# ── Tool formatters ──────────────────────────────────────────────────


def format_symbol(sym: Symbol, include_body: bool = True) -> str:
    """Format ast_get_symbol — single match."""
    has_body = include_body and sym.body
    lines = []

    # Location header — include size only when body is absent
    lines.append(_location(sym, include_size=not has_body))

    # Doc comment
    if sym.doc:
        lines.append(sym.doc)

    # Declaration
    decl = build_declaration(sym)

    if has_body:
        # Body already includes the declaration for functions/methods
        lines.append(sym.body)
    else:
        # Without body, show declaration with semicolon
        if sym.kind in ("class", "struct", "class_template"):
            lines.append(f"{decl} {{ ... }}")
        else:
            lines.append(f"{decl};")

    return "\n".join(lines)


def format_symbol_list(matches: list[Symbol], query_name: str) -> str:
    """Format ast_get_symbol — multiple matches."""
    lines = [f"{len(matches)} matches:"]
    for sym in matches:
        lines.append("")
        lines.append(_location(sym, include_size=True))
        lines.append(build_declaration(sym))
    return "\n".join(lines)


def format_search(query: str, results: list) -> str:
    """Format ast_search results.

    ``results`` is the list of SearchResult objects from SymbolSearch.
    """
    if not results:
        return f'No matches for "{query}".'

    lines = []
    for r in results:
        sym = r.symbol
        size = ""
        if sym.line_end > sym.line_start:
            size = f"  ({sym.line_end - sym.line_start + 1} lines)"

        # Compact declaration without param names (shorter for listings)
        decl = build_declaration(sym, include_param_names=False)

        lines.append(f"{sym.file}:{sym.line_start}  {sym.qualified_name}{size}")
        doc_part = ""
        if sym.doc:
            snippet = sym.doc[:200]
            doc_part = f"\n  {snippet}"
        lines.append(f"  {decl}{doc_part}")
    return "\n\n".join(lines)


def format_outline_class(cls: Symbol, members: list[Symbol]) -> str:
    """Format ast_get_outline — class path."""
    lines = []
    lines.append(_location(cls, include_size=True))
    if cls.doc:
        lines.append(cls.doc)

    decl = build_declaration(cls)
    lines.append(f"{decl} {{")

    for m in members:
        member_decl = build_declaration(m)
        if m.doc:
            lines.append(f"  {m.doc}")
        lines.append(f"  {member_decl};")

    lines.append("};")
    return "\n".join(lines)


def format_outline_file(name: str, symbols: list[Symbol]) -> str:
    """Format ast_get_outline — file path."""
    lines = [name, ""]
    for s in symbols:
        decl = build_declaration(s, include_param_names=True)
        size = ""
        if s.line_end > s.line_start:
            size = f"  ({s.line_end - s.line_start + 1} lines)"

        if s.doc:
            lines.append(f"{s.line_start:<4}  {s.doc}")
        lines.append(f"{'':4}  {decl}{size}")
    return "\n".join(lines)


def format_references(sym: Symbol, refs: list[dict], total: int) -> str:
    """Format ast_get_references."""
    lines = [f"{sym.qualified_name} \u2014 {total} references"]

    for ref in refs:
        lines.append("")
        caller = ref.get("caller", "")
        file = ref["file"]
        line = ref["line"]
        lines.append(f"{caller}  {file}:{line}")

        context = ref.get("context", "")
        if context:
            lines.append(context)
        else:
            ctx_line = ref.get("context_line", "")
            if ctx_line:
                lines.append(f"    {ctx_line}")

    return "\n".join(lines)


def format_hierarchy(cls: Symbol, bases: list[dict], derived: list[dict]) -> str:
    """Format ast_get_hierarchy."""
    lines = [f"{cls.qualified_name}  {cls.file}"]

    if bases:
        lines.append("  bases:")
        for b in bases:
            loc = ""
            if b.get("file"):
                loc = f"  {b['file']}"
                if b.get("line"):
                    loc += f":{b['line']}"
            lines.append(f"    {b['name']}{loc}")

    if derived:
        lines.append("  derived:")
        for d in derived:
            loc = ""
            if d.get("file"):
                loc = f"  {d['file']}"
                if d.get("line"):
                    loc += f":{d['line']}"
            lines.append(f"    {d['name']}{loc}")

    return "\n".join(lines)


def format_index(result: dict) -> str:
    """Format ast_index result."""
    stats = result.get("db_stats", {})
    files_total = result.get("files_total", result.get("files_indexed", 0))
    unchanged = result.get("files_unchanged", 0)
    errors = result.get("errors", 0)
    symbols = stats.get("symbols", 0)
    references = stats.get("references", 0)
    path = result.get("project_root", result.get("path", ""))
    return (
        f"Indexed {path}: {files_total} files "
        f"({unchanged} unchanged, {errors} errors), "
        f"{symbols} symbols, {references} references"
    )


def format_status(stats: dict) -> str:
    """Format ast_status result."""
    return (
        f"{stats.get('symbols', 0)} symbols, "
        f"{stats.get('references', 0)} references, "
        f"{stats.get('files', 0)} files"
    )


def format_error(message: str, suggestion: str = "") -> str:
    """Format an error response."""
    if suggestion:
        return f"{message} {suggestion}"
    return message
