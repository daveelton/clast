"""SQLite storage for the Clang AST index.

Stores symbols (functions, classes, variables) with their metadata,
source text, and cross-references. Designed for fast lookup by USR
(Unified Symbol Resolution) or qualified name.
"""

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class Symbol:
    usr: str
    name: str  # unqualified (e.g. "processBlock")
    qualified_name: str  # fully qualified (e.g. "MyPlugin::processBlock")
    kind: str  # "function", "method", "class", "field", "enum", etc.
    signature: str  # full signature string
    return_type: str
    parameters: list[dict]  # [{"name": "x", "type": "int"}, ...]
    attributes: list[str]  # ["override", "const", "noexcept", ...]
    parent_usr: str  # USR of parent scope (class, namespace)
    parent_name: str  # qualified name of parent
    file: str
    line_start: int
    line_end: int
    doc: str  # doc comment text
    body: str  # source text of the definition
    bases: list[str]  # for classes: list of base class USRs
    base_names: list[str]  # for classes: list of base class qualified names


@dataclass
class Reference:
    """A reference from one symbol to another."""
    referencing_usr: str  # the symbol that contains the reference
    referenced_usr: str  # the symbol being referenced
    file: str
    line: int
    context: str  # one-line snippet


@dataclass
class FileRecord:
    path: str
    mtime: float  # last modified time at indexing
    hash: str  # content hash for change detection


class ASTDatabase:
    """SQLite-backed storage for the AST index."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS symbols (
                usr TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                signature TEXT NOT NULL DEFAULT '',
                return_type TEXT NOT NULL DEFAULT '',
                parameters TEXT NOT NULL DEFAULT '[]',
                attributes TEXT NOT NULL DEFAULT '[]',
                parent_usr TEXT NOT NULL DEFAULT '',
                parent_name TEXT NOT NULL DEFAULT '',
                file TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                doc TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                bases TEXT NOT NULL DEFAULT '[]',
                base_names TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referencing_usr TEXT NOT NULL,
                referenced_usr TEXT NOT NULL,
                file TEXT NOT NULL,
                line INTEGER NOT NULL,
                context TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);
            CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
            CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent_usr);
            CREATE INDEX IF NOT EXISTS idx_refs_referenced ON refs(referenced_usr);
            CREATE INDEX IF NOT EXISTS idx_refs_referencing ON refs(referencing_usr);
            CREATE INDEX IF NOT EXISTS idx_refs_file ON refs(file);
        """)
        self.conn.commit()

        # Add unique index on refs, deduplicating existing data if needed
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_unique "
                "ON refs(referencing_usr, referenced_usr, file, line)"
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # Existing duplicates — deduplicate then create index
            self.conn.execute("""
                DELETE FROM refs WHERE id NOT IN (
                    SELECT MIN(id) FROM refs
                    GROUP BY referencing_usr, referenced_usr, file, line
                )
            """)
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_unique "
                "ON refs(referencing_usr, referenced_usr, file, line)"
            )
            self.conn.commit()

    # -- File tracking --

    def get_file(self, path: str) -> Optional[FileRecord]:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()
        if row:
            return FileRecord(path=row["path"], mtime=row["mtime"], hash=row["hash"])
        return None

    def upsert_file(self, rec: FileRecord):
        self.conn.execute(
            "INSERT OR REPLACE INTO files (path, mtime, hash) VALUES (?, ?, ?)",
            (rec.path, rec.mtime, rec.hash),
        )

    # -- Symbols --

    def upsert_symbol(self, sym: Symbol):
        self.conn.execute(
            """INSERT OR REPLACE INTO symbols
               (usr, name, qualified_name, kind, signature, return_type,
                parameters, attributes, parent_usr, parent_name,
                file, line_start, line_end, doc, body, bases, base_names)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sym.usr, sym.name, sym.qualified_name, sym.kind,
                sym.signature, sym.return_type,
                json.dumps(sym.parameters), json.dumps(sym.attributes),
                sym.parent_usr, sym.parent_name,
                sym.file, sym.line_start, sym.line_end,
                sym.doc, sym.body,
                json.dumps(sym.bases), json.dumps(sym.base_names),
            ),
        )

    def get_symbol_by_usr(self, usr: str) -> Optional[Symbol]:
        row = self.conn.execute("SELECT * FROM symbols WHERE usr = ?", (usr,)).fetchone()
        return self._row_to_symbol(row) if row else None

    def find_symbols_by_name(self, name: str) -> list[Symbol]:
        """Find symbols matching a name (unqualified or qualified, case-insensitive)."""
        rows = self.conn.execute(
            """SELECT * FROM symbols
               WHERE name LIKE ? OR qualified_name LIKE ?
               ORDER BY
                 CASE WHEN qualified_name = ? THEN 0
                      WHEN name = ? THEN 1
                      WHEN qualified_name LIKE ? THEN 2
                      ELSE 3
                 END
               LIMIT 20""",
            (f"%{name}%", f"%{name}%", name, name, f"%{name}"),
        ).fetchall()
        return [self._row_to_symbol(r) for r in rows]

    def get_class_members(self, class_usr: str) -> list[Symbol]:
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE parent_usr = ? ORDER BY line_start",
            (class_usr,),
        ).fetchall()
        return [self._row_to_symbol(r) for r in rows]

    def find_derived_classes(self, base_usr: str) -> list[Symbol]:
        """Find classes that list base_usr in their bases."""
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE kind = 'class' AND bases LIKE ?",
            (f'%"{base_usr}"%',),
        ).fetchall()
        return [self._row_to_symbol(r) for r in rows]

    def get_file_symbols(self, file_path: str) -> list[Symbol]:
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE file = ? ORDER BY line_start",
            (file_path,),
        ).fetchall()
        return [self._row_to_symbol(r) for r in rows]

    def get_all_symbols(self) -> list[Symbol]:
        rows = self.conn.execute("SELECT * FROM symbols").fetchall()
        return [self._row_to_symbol(r) for r in rows]

    # -- References --

    def add_reference(self, ref: Reference):
        self.conn.execute(
            """INSERT OR IGNORE INTO refs (referencing_usr, referenced_usr, file, line, context)
               VALUES (?, ?, ?, ?, ?)""",
            (ref.referencing_usr, ref.referenced_usr, ref.file, ref.line, ref.context),
        )

    def get_references_to(self, usr: str) -> list[dict]:
        """Get all references pointing to a symbol."""
        rows = self.conn.execute(
            """SELECT r.*, s.qualified_name as caller_name
               FROM refs r
               LEFT JOIN symbols s ON s.usr = r.referencing_usr
               WHERE r.referenced_usr = ?
               ORDER BY r.file, r.line""",
            (usr,),
        ).fetchall()
        return [
            {
                "caller": row["caller_name"] or row["referencing_usr"],
                "file": row["file"],
                "line": row["line"],
                "context": row["context"],
            }
            for row in rows
        ]

    # -- Bulk operations --

    def delete_file_data(self, file_path: str):
        """Remove all symbols and references from a file (for re-indexing)."""
        self.conn.execute("DELETE FROM refs WHERE file = ?", (file_path,))
        self.conn.execute("DELETE FROM symbols WHERE file = ?", (file_path,))
        self.conn.execute("DELETE FROM files WHERE path = ?", (file_path,))

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def stats(self) -> dict:
        syms = self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        refs = self.conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        files = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return {"symbols": syms, "references": refs, "files": files}

    # -- Internal --

    def _row_to_symbol(self, row: sqlite3.Row) -> Symbol:
        return Symbol(
            usr=row["usr"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            kind=row["kind"],
            signature=row["signature"],
            return_type=row["return_type"],
            parameters=json.loads(row["parameters"]),
            attributes=json.loads(row["attributes"]),
            parent_usr=row["parent_usr"],
            parent_name=row["parent_name"],
            file=row["file"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            doc=row["doc"],
            body=row["body"],
            bases=json.loads(row["bases"]),
            base_names=json.loads(row["base_names"]),
        )
