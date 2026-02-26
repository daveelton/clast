"""Clang AST indexer.

Parses C++ translation units using libclang, extracts symbols
(functions, classes, methods, fields, enums) with their metadata,
source text, doc comments, and cross-references.

Requires:
  - libclang shared library (e.g. libclang-18.so)
  - compile_commands.json from CMake
"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import clang.cindex as ci

from .db import ASTDatabase, FileRecord, Reference, Symbol

log = logging.getLogger(__name__)

# Cursor kinds we extract as symbols
SYMBOL_KINDS = {
    ci.CursorKind.FUNCTION_DECL: "function",
    ci.CursorKind.CXX_METHOD: "method",
    ci.CursorKind.CONSTRUCTOR: "constructor",
    ci.CursorKind.DESTRUCTOR: "destructor",
    ci.CursorKind.CLASS_DECL: "class",
    ci.CursorKind.STRUCT_DECL: "struct",
    ci.CursorKind.ENUM_DECL: "enum",
    ci.CursorKind.FIELD_DECL: "field",
    ci.CursorKind.VAR_DECL: "variable",
    ci.CursorKind.NAMESPACE: "namespace",
    ci.CursorKind.TYPEDEF_DECL: "typedef",
    ci.CursorKind.TYPE_ALIAS_DECL: "type_alias",
    ci.CursorKind.FUNCTION_TEMPLATE: "function_template",
    ci.CursorKind.CLASS_TEMPLATE: "class_template",
}

# Cursor kinds that represent references to other symbols
REF_KINDS = {
    ci.CursorKind.CALL_EXPR,
    ci.CursorKind.MEMBER_REF_EXPR,
    ci.CursorKind.DECL_REF_EXPR,
    ci.CursorKind.TYPE_REF,
    ci.CursorKind.CXX_BASE_SPECIFIER,
}

# Default libclang paths to try
LIBCLANG_PATHS = [
    "/usr/lib/x86_64-linux-gnu/libclang-18.so",
    "/usr/lib/llvm-18/lib/libclang.so",
    "/usr/lib/x86_64-linux-gnu/libclang-17.so",
    "/usr/lib/llvm-17/lib/libclang.so",
    "/opt/homebrew/opt/llvm/lib/libclang.dylib",  # macOS ARM
    "/usr/local/opt/llvm/lib/libclang.dylib",  # macOS Intel
]


def _find_libclang() -> str:
    """Find the libclang shared library."""
    # Check env var first
    env_path = os.environ.get("LIBCLANG_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    for p in LIBCLANG_PATHS:
        if Path(p).exists():
            return p
    raise RuntimeError(
        "libclang not found. Install libclang-dev or set LIBCLANG_PATH. "
        "Tried: " + ", ".join(LIBCLANG_PATHS)
    )


def _init_clang():
    """Initialize libclang once."""
    lib_path = _find_libclang()
    ci.Config.set_library_file(lib_path)
    log.info("Using libclang: %s", lib_path)


def _get_doc_comment(cursor) -> str:
    """Extract the doc comment (/// or /** */) for a cursor."""
    raw = cursor.raw_comment
    if not raw:
        return ""
    # Clean up comment markers
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("///"):
            lines.append(line[3:].strip())
        elif line.startswith("/**"):
            content = line[3:].strip()
            if content.endswith("*/"):
                content = content[:-2].strip()
            if content:
                lines.append(content)
        elif line.startswith("*/"):
            continue
        elif line.startswith("*"):
            lines.append(line[1:].strip())
        else:
            lines.append(line)
    return "\n".join(lines).strip()


def _get_qualified_name(cursor) -> str:
    """Build a fully qualified name like Namespace::Class::method."""
    parts = []
    c = cursor
    while c and c.kind != ci.CursorKind.TRANSLATION_UNIT:
        if c.spelling:
            parts.append(c.spelling)
        c = c.semantic_parent
    return "::".join(reversed(parts))


def _get_signature(cursor) -> str:
    """Build a human-readable signature string."""
    kind = cursor.kind

    if kind in (ci.CursorKind.CLASS_DECL, ci.CursorKind.STRUCT_DECL,
                ci.CursorKind.CLASS_TEMPLATE):
        bases = []
        for child in cursor.get_children():
            if child.kind == ci.CursorKind.CXX_BASE_SPECIFIER:
                access = child.access_specifier.name.lower()
                base_name = child.spelling or child.displayname
                if access == "public":
                    bases.append(f"public {base_name}")
                elif access == "protected":
                    bases.append(f"protected {base_name}")
                else:
                    bases.append(f"private {base_name}")
        kw = "struct" if kind == ci.CursorKind.STRUCT_DECL else "class"
        sig = f"{kw} {cursor.spelling}"
        if bases:
            sig += " : " + ", ".join(bases)
        return sig

    if kind in (ci.CursorKind.FUNCTION_DECL, ci.CursorKind.CXX_METHOD,
                ci.CursorKind.CONSTRUCTOR, ci.CursorKind.DESTRUCTOR,
                ci.CursorKind.FUNCTION_TEMPLATE):
        return cursor.displayname or cursor.spelling

    if kind == ci.CursorKind.FIELD_DECL:
        type_name = cursor.type.spelling if cursor.type else ""
        return f"{type_name} {cursor.spelling}"

    if kind == ci.CursorKind.VAR_DECL:
        type_name = cursor.type.spelling if cursor.type else ""
        return f"{type_name} {cursor.spelling}"

    if kind == ci.CursorKind.ENUM_DECL:
        return f"enum {cursor.spelling}" if cursor.spelling else "enum (anonymous)"

    return cursor.displayname or cursor.spelling or ""


def _get_return_type(cursor) -> str:
    """Get the return type of a function/method."""
    if cursor.kind in (ci.CursorKind.FUNCTION_DECL, ci.CursorKind.CXX_METHOD,
                        ci.CursorKind.FUNCTION_TEMPLATE):
        if cursor.result_type:
            return cursor.result_type.spelling
    return ""


def _get_parameters(cursor) -> list[dict]:
    """Extract parameter names and types."""
    params = []
    for child in cursor.get_children():
        if child.kind == ci.CursorKind.PARM_DECL:
            params.append({
                "name": child.spelling or "",
                "type": child.type.spelling if child.type else "",
            })
    return params


def _get_attributes(cursor) -> list[str]:
    """Detect method attributes like override, const, noexcept, virtual, static."""
    attrs = []
    if cursor.kind == ci.CursorKind.CXX_METHOD:
        if cursor.is_const_method():
            attrs.append("const")
        if cursor.is_virtual_method():
            attrs.append("virtual")
        if cursor.is_pure_virtual_method():
            attrs.append("pure_virtual")
        if cursor.is_static_method():
            attrs.append("static")
        # Check for override in tokens
        try:
            tokens = list(cursor.get_tokens())
            token_spellings = [t.spelling for t in tokens]
            if "override" in token_spellings:
                attrs.append("override")
            if "noexcept" in token_spellings:
                attrs.append("noexcept")
            if "final" in token_spellings:
                attrs.append("final")
        except Exception:
            pass
    return attrs


def _get_bases(cursor) -> tuple[list[str], list[str]]:
    """Get base class USRs and names for a class cursor."""
    base_usrs = []
    base_names = []
    for child in cursor.get_children():
        if child.kind == ci.CursorKind.CXX_BASE_SPECIFIER:
            ref = child.get_definition()
            if ref:
                base_usrs.append(ref.get_usr())
                base_names.append(_get_qualified_name(ref))
            else:
                # Can't resolve definition (e.g., in a third-party header)
                base_names.append(child.spelling or child.displayname)
                base_usrs.append("")
    return base_usrs, base_names


def _extract_source(cursor, source_lines: list[str]) -> str:
    """Extract the source text for a cursor from the file's line cache."""
    extent = cursor.extent
    if not extent or not extent.start.file:
        return ""
    start_line = extent.start.line - 1  # 0-indexed
    end_line = extent.end.line  # exclusive
    if start_line < 0 or end_line > len(source_lines):
        return ""
    return "\n".join(source_lines[start_line:end_line])


def _get_line_at(line_num: int, source_lines: list[str]) -> str:
    """Get a single source line (1-indexed)."""
    idx = line_num - 1
    if 0 <= idx < len(source_lines):
        return source_lines[idx].strip()
    return ""


class Indexer:
    """Indexes C++ source files using libclang."""

    def __init__(self, db: ASTDatabase, compile_commands_dir: str | None = None,
                 project_root: str | None = None):
        _init_clang()
        self.db = db
        self.index = ci.Index.create()
        self.compile_commands_dir = compile_commands_dir
        self._compdb = None

        # Project root: headers under this path are indexed alongside .cpp files.
        # Falls back to parent of compile_commands_dir (e.g. cmake-build-debug/..)
        if project_root:
            self.project_root = str(Path(project_root).resolve()) + os.sep
        elif compile_commands_dir:
            self.project_root = str(Path(compile_commands_dir).resolve().parent) + os.sep
        else:
            self.project_root = None

        # Track headers already indexed in this session to avoid redundant work.
        # Each header is fully indexed by the first .cpp that includes it;
        # subsequent .cpp files skip the header's subtree (references FROM .cpp
        # files are still recorded since those cursors are in the .cpp itself).
        self._indexed_headers: set[str] = set()

        if compile_commands_dir:
            try:
                self._compdb = ci.CompilationDatabase.fromDirectory(compile_commands_dir)
                log.info("Loaded compile_commands.json from %s", compile_commands_dir)
            except ci.CompilationDatabaseError:
                log.warning("No compile_commands.json found in %s", compile_commands_dir)

    def _get_compile_args(self, filepath: str) -> list[str]:
        """Get compile arguments for a file from compile_commands.json."""
        if self._compdb:
            try:
                commands = self._compdb.getCompileCommands(filepath)
                if commands:
                    # Extract args, skip the compiler binary and source file
                    args = []
                    cmd = list(commands[0].arguments)
                    for i, arg in enumerate(cmd):
                        if i == 0:
                            continue  # skip compiler
                        if arg in ("-c", "-o") or (i > 0 and cmd[i - 1] == "-o"):
                            continue
                        if arg == filepath or arg.endswith(Path(filepath).name):
                            continue
                        args.append(arg)
                    return args
            except Exception as e:
                log.debug("Failed to get compile commands for %s: %s", filepath, e)
        # Fallback: basic C++17 flags
        return ["-std=c++17", "-x", "c++"]

    def index_file(self, filepath: str, force: bool = False) -> dict:
        """Index a single C++ source file.

        Returns stats dict with counts of symbols and references extracted.
        """
        filepath = str(Path(filepath).resolve())

        # Check if file needs re-indexing
        if not force:
            content = Path(filepath).read_bytes()
            content_hash = hashlib.sha256(content).hexdigest()
            existing = self.db.get_file(filepath)
            if existing and existing.hash == content_hash:
                log.debug("Skipping unchanged file: %s", filepath)
                return {"file": filepath, "status": "unchanged"}

        log.debug("Indexing: %s", filepath)

        # Read source for body extraction
        source_text = Path(filepath).read_text(errors="replace")
        source_lines = source_text.splitlines()
        content_hash = hashlib.sha256(source_text.encode()).hexdigest()

        # Parse with clang
        args = self._get_compile_args(filepath)
        tu = self.index.parse(
            filepath,
            args=args,
            options=(
                ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                | ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES * 0  # we want bodies
            ),
        )

        if not tu:
            log.error("Failed to parse: %s", filepath)
            return {"file": filepath, "status": "parse_error"}

        # Log diagnostics (but don't fail - partial ASTs are still useful)
        errors = [d for d in tu.diagnostics if d.severity >= ci.Diagnostic.Error]
        if errors:
            log.warning("%s: %d errors (indexing partial AST)", filepath, len(errors))
            for e in errors[:3]:
                log.debug("  %s", e)

        # Clear old data for this file
        self.db.delete_file_data(filepath)

        # Walk the AST — collect symbols and refs in memory, then batch-write
        sym_count = 0
        ref_count = 0
        pending_symbols: list[Symbol] = []
        pending_refs: list[Reference] = []

        # Source line cache: read header files on demand for body extraction
        source_cache: dict[str, list[str]] = {filepath: source_lines}

        def get_source_lines(file_path: str) -> list[str]:
            if file_path not in source_cache:
                try:
                    text = Path(file_path).read_text(errors="replace")
                    source_cache[file_path] = text.splitlines()
                except Exception:
                    source_cache[file_path] = []
            return source_cache[file_path]

        # Track which function/method we're currently inside for reference context
        scope_stack: list[str] = []  # stack of USRs

        def visit(cursor, depth=0):
            nonlocal sym_count, ref_count

            # Skip cursors outside the project, and skip headers already indexed
            cursor_file = os.path.realpath(str(cursor.location.file)) if cursor.location.file else None
            if cursor_file and cursor_file != filepath:
                if not self.project_root or not cursor_file.startswith(self.project_root):
                    return
                if cursor_file in self._indexed_headers:
                    return
            effective_file = cursor_file or filepath

            kind = cursor.kind
            kind_str = SYMBOL_KINDS.get(kind)

            if kind_str:
                # This is a symbol definition
                usr = cursor.get_usr()
                if not usr:
                    # Skip anonymous symbols we can't reference
                    for child in cursor.get_children():
                        visit(child, depth + 1)
                    return

                # Only index definitions, not forward declarations.
                # For classes, is_definition() is True for the full declaration
                # with members (class Foo { ... }) and False for forward
                # declarations (class Foo;). We skip forward declarations to
                # prevent them from overwriting the real definition via upsert.
                is_definition = cursor.is_definition()
                is_class_like = kind in (
                    ci.CursorKind.CLASS_DECL, ci.CursorKind.STRUCT_DECL,
                    ci.CursorKind.ENUM_DECL, ci.CursorKind.CLASS_TEMPLATE,
                )

                if is_definition:
                    # Get parent scope
                    parent = cursor.semantic_parent
                    parent_usr = ""
                    parent_name = ""
                    if parent and parent.kind != ci.CursorKind.TRANSLATION_UNIT:
                        parent_usr = parent.get_usr() or ""
                        parent_name = _get_qualified_name(parent)

                    bases_usrs, bases_names = ([], [])
                    if is_class_like:
                        bases_usrs, bases_names = _get_bases(cursor)

                    lines = get_source_lines(effective_file)
                    body = _extract_source(cursor, lines) if is_definition else ""

                    sym = Symbol(
                        usr=usr,
                        name=cursor.spelling,
                        qualified_name=_get_qualified_name(cursor),
                        kind=kind_str,
                        signature=_get_signature(cursor),
                        return_type=_get_return_type(cursor),
                        parameters=_get_parameters(cursor),
                        attributes=_get_attributes(cursor),
                        parent_usr=parent_usr,
                        parent_name=parent_name,
                        file=effective_file,
                        line_start=cursor.extent.start.line if cursor.extent else 0,
                        line_end=cursor.extent.end.line if cursor.extent else 0,
                        doc=_get_doc_comment(cursor),
                        body=body,
                        bases=bases_usrs,
                        base_names=bases_names,
                    )
                    pending_symbols.append(sym)
                    sym_count += 1

                # Push this symbol as current scope for reference tracking
                scope_stack.append(usr)
                for child in cursor.get_children():
                    visit(child, depth + 1)
                scope_stack.pop()
                return

            # Check for references
            if kind in REF_KINDS:
                ref_cursor = cursor.referenced
                if ref_cursor:
                    ref_usr = ref_cursor.get_usr()
                    if ref_usr and scope_stack:
                        line = cursor.location.line
                        lines = get_source_lines(effective_file)
                        context = _get_line_at(line, lines)
                        ref = Reference(
                            referencing_usr=scope_stack[-1],
                            referenced_usr=ref_usr,
                            file=effective_file,
                            line=line,
                            context=context,
                        )
                        pending_refs.append(ref)
                        ref_count += 1

            # Recurse into children
            for child in cursor.get_children():
                visit(child, depth + 1)

        visit(tu.cursor)

        # Batch-write all collected data in a single transaction
        header_files = {hf for hf in source_cache if hf != filepath}
        self.db.delete_file_data(filepath)
        for sym in pending_symbols:
            self.db.upsert_symbol(sym)
        for ref in pending_refs:
            self.db.add_reference(ref)

        # Mark visited headers so subsequent .cpp files skip them
        self._indexed_headers.update(header_files)

        # Record the file
        self.db.upsert_file(FileRecord(
            path=filepath,
            mtime=os.path.getmtime(filepath),
            hash=content_hash,
        ))
        self.db.commit()

        return {
            "file": filepath,
            "status": "indexed",
            "symbols": sym_count,
            "references": ref_count,
            "errors": len(errors),
        }

    def index_directory(
        self, directory: str, extensions: tuple[str, ...] = (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"),
        force: bool = False,
    ) -> dict:
        """Index all C++ files in a directory recursively.

        Returns aggregate stats.
        """
        directory = str(Path(directory).resolve())

        # Collect files first so we can show progress
        all_files = []
        for root, _dirs, files in os.walk(directory):
            for fname in sorted(files):
                if any(fname.endswith(ext) for ext in extensions):
                    all_files.append(os.path.join(root, fname))

        total = len(all_files)
        log.info("Found %d files to index", total)
        results = []
        for i, fpath in enumerate(all_files, 1):
            try:
                log.info("[%d/%d] %s", i, total, Path(fpath).name)
                result = self.index_file(fpath, force=force)
                results.append(result)
            except Exception as e:
                log.error("Failed to index %s: %s", fpath, e)
                results.append({"file": fpath, "status": "error", "error": str(e)})

        indexed = sum(1 for r in results if r["status"] == "indexed")
        unchanged = sum(1 for r in results if r.get("status") == "unchanged")
        errored = sum(1 for r in results if r.get("status") in ("error", "parse_error"))
        total_syms = sum(r.get("symbols", 0) for r in results)
        total_refs = sum(r.get("references", 0) for r in results)

        return {
            "directory": directory,
            "files_indexed": indexed,
            "files_unchanged": unchanged,
            "files_errored": errored,
            "total_symbols": total_syms,
            "total_references": total_refs,
        }

    def index_from_compile_commands(self, force: bool = False) -> dict:
        """Index all files listed in compile_commands.json."""
        if not self._compdb or not self.compile_commands_dir:
            return {"error": "No compile_commands.json loaded"}

        cc_path = Path(self.compile_commands_dir) / "compile_commands.json"
        if not cc_path.exists():
            return {"error": f"compile_commands.json not found at {cc_path}"}

        with open(cc_path) as f:
            entries = json.load(f)

        files = [entry["file"] for entry in entries if "file" in entry]
        total = len(files)
        log.info("Found %d files to index", total)
        results = []
        for i, fpath in enumerate(files, 1):
            try:
                log.info("[%d/%d] %s", i, total, Path(fpath).name)
                result = self.index_file(fpath, force=force)
                results.append(result)
            except Exception as e:
                log.error("Failed to index %s: %s", fpath, e)
                results.append({"file": fpath, "status": "error", "error": str(e)})

        indexed = sum(1 for r in results if r["status"] == "indexed")
        unchanged = sum(1 for r in results if r.get("status") == "unchanged")
        total_syms = sum(r.get("symbols", 0) for r in results)
        total_refs = sum(r.get("references", 0) for r in results)

        return {
            "source": str(cc_path),
            "files_indexed": indexed,
            "files_unchanged": unchanged,
            "total_symbols": total_syms,
            "total_references": total_refs,
        }
