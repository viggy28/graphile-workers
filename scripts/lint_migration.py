#!/usr/bin/env python3
"""
lint_migration.py — flag risky Postgres migration patterns.

Usage:
    lint_migration.py file1.sql [file2.sql ...]            # human output
    lint_migration.py --format=json file1.sql              # JSON for CI
    lint_migration.py --strict file1.sql                   # exit 1 on warnings too
    cat file.sql | lint_migration.py --stdin               # read from stdin

Exit codes:
    0   no issues (or only warnings, without --strict)
    1   errors found, or warnings found with --strict
    2   parse failure / usage error

Requires: pglast (pip install pglast)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

try:
    from pglast import parse_sql
    from pglast import ast
    from pglast.enums import AlterTableType
except ImportError:
    print(
        "error: pglast is required. Install with: pip install pglast",
        file=sys.stderr,
    )
    sys.exit(2)


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# Postgres functions considered volatile for ADD COLUMN ... DEFAULT purposes.
# A non-exhaustive list of the common offenders that force a table rewrite.
VOLATILE_FUNCTIONS = {
    "now",
    "current_timestamp",
    "clock_timestamp",
    "transaction_timestamp",
    "statement_timestamp",
    "random",
    "gen_random_uuid",
    "uuid_generate_v1",
    "uuid_generate_v1mc",
    "uuid_generate_v4",
    "nextval",
}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    file: str
    statement_index: int  # 1-based: which statement in the file


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def stmt_type_name(node) -> str:
    return type(node).__name__


def iter_cmds(alter_stmt: ast.AlterTableStmt):
    """Yield (cmd_node, subtype_enum) for each command in an ALTER TABLE."""
    for cmd in alter_stmt.cmds or ():
        yield cmd, cmd.subtype


def find_volatile_call(expr) -> str | None:
    """If the expression tree contains a volatile function call, return its name."""
    if expr is None:
        return None
    if isinstance(expr, ast.FuncCall):
        # funcname is a tuple of String nodes; the last is the bare function name
        if expr.funcname:
            name = expr.funcname[-1].sval.lower()
            if name in VOLATILE_FUNCTIONS:
                return name
    # Recurse into children. pglast nodes expose attributes via __dict__.
    for attr_name in getattr(expr, "__slots__", ()) or ():
        child = getattr(expr, attr_name, None)
        if isinstance(child, (list, tuple)):
            for c in child:
                if hasattr(c, "__slots__"):
                    found = find_volatile_call(c)
                    if found:
                        return found
        elif hasattr(child, "__slots__"):
            found = find_volatile_call(child)
            if found:
                return found
    return None


def column_has_not_null(coldef: ast.ColumnDef) -> bool:
    for c in coldef.constraints or ():
        if c.contype.name == "CONSTR_NOTNULL":
            return True
    return False


def column_default_expr(coldef: ast.ColumnDef):
    for c in coldef.constraints or ():
        if c.contype.name == "CONSTR_DEFAULT":
            return c.raw_expr
    return None


# ---------------------------------------------------------------------------
# Rule checks — one function per rule, all return list[Finding]
# ---------------------------------------------------------------------------

def check_index_concurrent(stmt, idx: int, file: str) -> list[Finding]:
    """CREATE/DROP INDEX without CONCURRENTLY."""
    findings = []
    if isinstance(stmt, ast.IndexStmt):
        if not stmt.concurrent:
            findings.append(Finding(
                rule="INDEX_NOT_CONCURRENT",
                severity=SEVERITY_ERROR,
                message=(
                    "CREATE INDEX without CONCURRENTLY takes a SHARE lock and "
                    "blocks all writes for the duration. Use CREATE INDEX CONCURRENTLY."
                ),
                file=file, statement_index=idx,
            ))
    elif isinstance(stmt, ast.DropStmt):
        # removeType is an OBJECT_* enum; we want OBJECT_INDEX
        if stmt.removeType.name == "OBJECT_INDEX" and not stmt.concurrent:
            findings.append(Finding(
                rule="INDEX_NOT_CONCURRENT",
                severity=SEVERITY_ERROR,
                message=(
                    "DROP INDEX without CONCURRENTLY takes ACCESS EXCLUSIVE "
                    "and queues behind/blocks other queries. Use DROP INDEX CONCURRENTLY."
                ),
                file=file, statement_index=idx,
            ))
    return findings


def check_alter_table(stmt, idx: int, file: str) -> list[Finding]:
    findings = []
    if not isinstance(stmt, ast.AlterTableStmt):
        return findings

    for cmd, subtype in iter_cmds(stmt):
        name = subtype.name

        # AT_AddColumn
        if name == "AT_AddColumn":
            coldef = cmd.def_
            if not isinstance(coldef, ast.ColumnDef):
                continue
            not_null = column_has_not_null(coldef)
            default_expr = column_default_expr(coldef)

            if not_null and default_expr is None:
                findings.append(Finding(
                    rule="ADD_COLUMN_NOT_NULL_NO_DEFAULT",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"ADD COLUMN {coldef.colname} NOT NULL with no DEFAULT requires a "
                        "full table scan (pre-PG11: rewrite). Add nullable, backfill, "
                        "add CHECK NOT VALID, VALIDATE, then SET NOT NULL."
                    ),
                    file=file, statement_index=idx,
                ))
            if default_expr is not None:
                vol = find_volatile_call(default_expr)
                if vol:
                    findings.append(Finding(
                        rule="ADD_COLUMN_VOLATILE_DEFAULT",
                        severity=SEVERITY_ERROR,
                        message=(
                            f"ADD COLUMN {coldef.colname} DEFAULT uses volatile "
                            f"function {vol}() — forces a full table rewrite. "
                            "Add nullable, backfill in batches, then set the default."
                        ),
                        file=file, statement_index=idx,
                    ))

        # AT_AlterColumnType
        elif name == "AT_AlterColumnType":
            findings.append(Finding(
                rule="COLUMN_TYPE_CHANGE",
                severity=SEVERITY_WARNING,
                message=(
                    f"ALTER COLUMN {cmd.name} TYPE typically rewrites the table and "
                    "always rewrites indexes. Use expand/contract: new column, dual-write, backfill, swap."
                ),
                file=file, statement_index=idx,
            ))

        # AT_SetNotNull
        elif name == "AT_SetNotNull":
            findings.append(Finding(
                rule="SET_NOT_NULL_NO_CHECK",
                severity=SEVERITY_WARNING,
                message=(
                    f"ALTER COLUMN {cmd.name} SET NOT NULL scans the full table under "
                    "ACCESS EXCLUSIVE. On PG12+, first add CHECK (col IS NOT NULL) NOT VALID, "
                    "VALIDATE CONSTRAINT, then SET NOT NULL uses the proof and skips the scan."
                ),
                file=file, statement_index=idx,
            ))

        # AT_AddConstraint — FK or CHECK without NOT VALID
        elif name == "AT_AddConstraint":
            con = cmd.def_
            if not isinstance(con, ast.Constraint):
                continue
            contype = con.contype.name
            if contype in ("CONSTR_CHECK", "CONSTR_FOREIGN") and not con.skip_validation:
                kind = "CHECK" if contype == "CONSTR_CHECK" else "FOREIGN KEY"
                findings.append(Finding(
                    rule="CONSTRAINT_NO_NOT_VALID",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"ADD {kind} CONSTRAINT without NOT VALID requires a full table scan "
                        "under ACCESS EXCLUSIVE. Add NOT VALID, then VALIDATE CONSTRAINT "
                        "(takes SHARE UPDATE EXCLUSIVE only)."
                    ),
                    file=file, statement_index=idx,
                ))

        # AT_DropColumn
        elif name == "AT_DropColumn":
            findings.append(Finding(
                rule="DROP_COLUMN",
                severity=SEVERITY_WARNING,
                message=(
                    f"DROP COLUMN {cmd.name}: metadata-only but breaks any app code "
                    "still referencing it, and the data isn't reclaimed until VACUUM FULL "
                    "or pg_repack. Confirm reads and writes are stopped first."
                ),
                file=file, statement_index=idx,
            ))

        # AT_SetTableSpace
        elif name == "AT_SetTableSpace":
            findings.append(Finding(
                rule="SET_TABLESPACE",
                severity=SEVERITY_ERROR,
                message=(
                    "ALTER TABLE ... SET TABLESPACE rewrites the entire table under "
                    "ACCESS EXCLUSIVE. Use pg_repack --tablespace instead."
                ),
                file=file, statement_index=idx,
            ))

        # AT_DetachPartition
        elif name == "AT_DetachPartition":
            if not getattr(cmd, "concurrent", False):
                findings.append(Finding(
                    rule="DETACH_NOT_CONCURRENT",
                    severity=SEVERITY_ERROR,
                    message=(
                        "DETACH PARTITION without CONCURRENTLY takes ACCESS EXCLUSIVE on "
                        "both parent and partition. Use DETACH PARTITION CONCURRENTLY (PG14+)."
                    ),
                    file=file, statement_index=idx,
                ))

    return findings


def check_rename(stmt, idx: int, file: str) -> list[Finding]:
    """RENAME TABLE / RENAME COLUMN — app-coupling hazard."""
    if not isinstance(stmt, ast.RenameStmt):
        return []
    rt = stmt.renameType.name
    if rt in ("OBJECT_TABLE", "OBJECT_COLUMN"):
        kind = "table" if rt == "OBJECT_TABLE" else "column"
        return [Finding(
            rule="RENAME",
            severity=SEVERITY_WARNING,
            message=(
                f"Renaming a {kind} is fast at the DB level but breaks any app code "
                "still referencing the old name. Use expand/contract: add new, dual-write, "
                "switch readers, drop old."
            ),
            file=file, statement_index=idx,
        )]
    return []


def check_reindex(stmt, idx: int, file: str) -> list[Finding]:
    if not isinstance(stmt, ast.ReindexStmt):
        return []
    # ReindexStmt.params is a list of DefElem; CONCURRENTLY shows up there in newer parsers
    concurrent = False
    for p in getattr(stmt, "params", None) or ():
        if getattr(p, "defname", "") == "concurrently":
            concurrent = True
    if not concurrent:
        return [Finding(
            rule="REINDEX_NOT_CONCURRENT",
            severity=SEVERITY_ERROR,
            message="REINDEX without CONCURRENTLY blocks reads and writes. Use REINDEX ... CONCURRENTLY (PG12+).",
            file=file, statement_index=idx,
        )]
    return []


# ---------------------------------------------------------------------------
# File-level checks (not per-statement)
# ---------------------------------------------------------------------------

LOCK_TIMEOUT_RE = re.compile(
    r"\bSET\s+(?:LOCAL\s+)?lock_timeout\s*(?:=|TO)\s*", re.IGNORECASE
)


def check_missing_lock_timeout(sql_text: str, file: str) -> list[Finding]:
    """Warn if no SET lock_timeout in the file at all."""
    if LOCK_TIMEOUT_RE.search(sql_text):
        return []
    return [Finding(
        rule="MISSING_LOCK_TIMEOUT",
        severity=SEVERITY_WARNING,
        message=(
            "No SET lock_timeout found. Without it, an ALTER waiting on a long-running "
            "query will block every other query on the table. Add SET lock_timeout = '2s' "
            "at the top, or configure it as a role default."
        ),
        file=file, statement_index=0,
    )]


def check_concurrent_with_other_stmts(parsed, file: str) -> list[Finding]:
    """golang-migrate-specific: CONCURRENTLY mixed with other statements fails at runtime.

    Postgres refuses CONCURRENTLY inside a transaction, and migration tools that
    wrap the file in one will explode if there's anything else alongside.
    """
    has_concurrent = False
    other_stmt_count = 0
    for raw in parsed:
        s = raw.stmt
        if isinstance(s, ast.IndexStmt) and s.concurrent:
            has_concurrent = True
        elif isinstance(s, ast.DropStmt) and getattr(s, "concurrent", False):
            has_concurrent = True
        elif isinstance(s, ast.ReindexStmt):
            for p in getattr(s, "params", None) or ():
                if getattr(p, "defname", "") == "concurrently":
                    has_concurrent = True
        else:
            # SET statements don't count — they're session-level metadata that
            # doesn't open a transaction by itself.
            if not isinstance(s, ast.VariableSetStmt):
                other_stmt_count += 1
    if has_concurrent and other_stmt_count > 0:
        return [Finding(
            rule="CONCURRENT_WITH_OTHER_STMTS",
            severity=SEVERITY_ERROR,
            message=(
                "File mixes a CONCURRENTLY statement with other non-SET statements. "
                "golang-migrate (and similar tools) will wrap the file in a transaction "
                "and Postgres will reject the CONCURRENTLY. Put each CONCURRENTLY statement "
                "in its own migration file."
            ),
            file=file, statement_index=0,
        )]
    return []


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def lint_sql(sql_text: str, file: str) -> list[Finding]:
    try:
        parsed = parse_sql(sql_text)
    except Exception as e:
        return [Finding(
            rule="PARSE_ERROR",
            severity=SEVERITY_ERROR,
            message=f"Failed to parse SQL: {e}",
            file=file, statement_index=0,
        )]

    findings: list[Finding] = []
    findings.extend(check_missing_lock_timeout(sql_text, file))
    findings.extend(check_concurrent_with_other_stmts(parsed, file))

    for i, raw in enumerate(parsed, start=1):
        s = raw.stmt
        findings.extend(check_index_concurrent(s, i, file))
        findings.extend(check_alter_table(s, i, file))
        findings.extend(check_rename(s, i, file))
        findings.extend(check_reindex(s, i, file))

    return findings


def format_human(findings: list[Finding]) -> str:
    if not findings:
        return ""
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)
    out: list[str] = []
    for fname, fs in by_file.items():
        out.append(f"\n{fname}")
        out.append("─" * max(len(fname), 40))
        for f in fs:
            tag = "✗" if f.severity == SEVERITY_ERROR else "!"
            loc = f"stmt {f.statement_index}" if f.statement_index else "file"
            out.append(f"  {tag} [{f.severity}] {f.rule} ({loc})")
            for line in f.message.split("\n"):
                out.append(f"      {line}")
    return "\n".join(out)


def format_json(findings: list[Finding]) -> str:
    return json.dumps([asdict(f) for f in findings], indent=2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Lint Postgres migrations for risky patterns.")
    p.add_argument("files", nargs="*", help="SQL files to lint")
    p.add_argument("--stdin", action="store_true", help="read SQL from stdin (file shown as <stdin>)")
    p.add_argument("--format", choices=("human", "json"), default="human")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 on warnings too (default: only errors)")
    args = p.parse_args(argv)

    inputs: list[tuple[str, str]] = []
    if args.stdin:
        inputs.append(("<stdin>", sys.stdin.read()))
    for path in args.files:
        try:
            inputs.append((path, Path(path).read_text()))
        except OSError as e:
            print(f"error reading {path}: {e}", file=sys.stderr)
            return 2

    if not inputs:
        p.print_usage(sys.stderr)
        return 2

    all_findings: list[Finding] = []
    for fname, text in inputs:
        all_findings.extend(lint_sql(text, fname))

    if args.format == "json":
        print(format_json(all_findings))
    else:
        out = format_human(all_findings)
        if out:
            print(out)
            print()
        print(
            f"{sum(1 for f in all_findings if f.severity == SEVERITY_ERROR)} error(s), "
            f"{sum(1 for f in all_findings if f.severity == SEVERITY_WARNING)} warning(s) "
            f"across {len(inputs)} file(s)"
        )

    has_error = any(f.severity == SEVERITY_ERROR for f in all_findings)
    has_warning = any(f.severity == SEVERITY_WARNING for f in all_findings)
    if has_error or (args.strict and has_warning):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
