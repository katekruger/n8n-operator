"""Contract: the schema and its migrations use only portable constructs (ADR-004).

Binding rules D1-D10 exist so the v2 move from SQLite to PostgreSQL is a configuration
change, not a rewrite. Most of them are structural properties of ``storage/models.py``
that are cheapest to check by inspecting the SQLAlchemy metadata directly (D1-D3, D8,
D10); a few (D4, D5, D6) are about *how code is written* and are checked with a small
AST-based scan over actual string literals and function calls — deliberately not a plain
substring search over the whole file, since this module's own docstrings quote the very
constructs (``RETURNING``, ``create_all``) they document as forbidden, which a naive grep
would misreport as violations.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String
from sqlalchemy.types import TypeDecorator

from n8n_operator.storage.models import AuditLogEntry, Base, UTCDateTime

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE = REPO_ROOT / "src" / "n8n_operator" / "storage"
MIGRATIONS_DIR = STORAGE / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"

# The one deliberate, documented exception to "primary keys are ULIDs" (D1): the audit
# chain needs strict monotonic ordering a ULID does not guarantee under concurrency.
TABLES_WITH_INTEGER_PK = {"audit_log"}


def _tables() -> list[str]:
    return sorted(Base.metadata.tables.keys())


def _non_docstring_string_literals(path: Path) -> list[str]:
    """Every string literal in ``path`` that is *not* a module/class/function docstring.

    Module docstrings throughout this codebase quote the very SQL constructs ADR-004
    forbids, in order to explain the rule — including that text in a search for
    violations would make the test fail on its own documentation. Real string literals
    used as arguments (a PRAGMA, a SQL fragment) are unaffected by this exclusion.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstring_nodes.add(id(first.value))

    literals: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_nodes
        ):
            literals.append(node.value)
    return literals


def _calls_named(path: Path, attribute_name: str) -> bool:
    """True if ``path`` contains a call to ``<anything>.<attribute_name>(...)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute_name
        ):
            return True
    return False


def _imports_name_from_sqlalchemy(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "sqlalchemy"
            and any(alias.name == name for alias in node.names)
        ):
            return True
    return False


# --------------------------------------------------------------------------------------
# D1 — ULID string primary keys, never AUTOINCREMENT/SERIAL, with one named exception.
# --------------------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.parametrize("table_name", _tables())
def test_primary_keys_are_string_ulids_except_the_documented_exception(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    pk_columns = list(table.primary_key.columns)
    assert pk_columns, f"{table_name} has no primary key"

    if table_name in TABLES_WITH_INTEGER_PK:
        for col in pk_columns:
            assert isinstance(col.type, Integer), (
                f"{table_name}.{col.name} is the documented integer-PK exception "
                f"but is not an Integer column"
            )
            assert col.autoincrement is True or col.autoincrement == "auto"
        return

    for col in pk_columns:
        assert isinstance(col.type, String), f"{table_name}.{col.name} PK is not a String/ULID"
        # A String-typed column is never DB-generated the way an Integer identity would
        # be — this just confirms nothing has set autoincrement on it regardless.
        assert col.autoincrement in (False, "auto", None)


@pytest.mark.contract
def test_audit_log_is_the_only_integer_primary_key_table() -> None:
    integer_pk_tables = {
        name
        for name, table in Base.metadata.tables.items()
        if any(isinstance(c.type, Integer) for c in table.primary_key.columns)
    }
    assert integer_pk_tables == TABLES_WITH_INTEGER_PK


# --------------------------------------------------------------------------------------
# D2 — every timestamp is DateTime(timezone=True); never naive, never a string.
#
# ``UTCDateTime`` (storage/models.py) is a ``TypeDecorator`` wrapping ``DateTime``, so
# ``column.type`` is a ``UTCDateTime`` instance, not a ``DateTime`` instance directly —
# ``isinstance(column.type, DateTime)`` is False for every one of them (confirmed
# empirically; a naive version of this check would silently check nothing at all). The
# real, underlying SQL type is ``column.type.impl``.
# --------------------------------------------------------------------------------------

_EXPECTED_DATETIME_COLUMNS = {
    ("approvals", "issued_at"),
    ("approvals", "expires_at"),
    ("approvals", "decided_at"),
    ("audit_log", "occurred_at"),
    ("execution_results", "started_at"),
    ("execution_results", "finished_at"),
    ("operation_events", "occurred_at"),
    ("operations", "handle_burned_at"),
    ("operations", "approval_expires_at"),
    ("operations", "execution_deadline"),
    ("operations", "created_at"),
    ("operations", "updated_at"),
    ("principals", "created_at"),
    ("registry_snapshots", "loaded_at"),
}


@pytest.mark.contract
def test_every_datetime_column_uses_the_utc_aware_type_decorator() -> None:
    """No column may use bare ``DateTime(timezone=True)`` directly: on SQLite that
    declaration alone does not survive a round trip with its tzinfo intact (see
    ``UTCDateTime``'s docstring) — every timestamp column must go through it instead."""
    found: set[tuple[str, str]] = set()
    for table_name, table in Base.metadata.tables.items():
        for column in table.columns:
            if type(column.type) is UTCDateTime:
                found.add((table_name, column.name))
            elif isinstance(column.type, DateTime):
                pytest.fail(
                    f"{table_name}.{column.name} uses bare DateTime directly — "
                    f"use UTCDateTime instead (ADR-004 rule D2)"
                )
    assert found == _EXPECTED_DATETIME_COLUMNS


@pytest.mark.contract
@pytest.mark.parametrize("table_name", _tables())
def test_datetime_columns_are_all_timezone_aware(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    for column in table.columns:
        effective_type = column.type.impl if isinstance(column.type, TypeDecorator) else column.type
        if isinstance(effective_type, DateTime):
            assert effective_type.timezone is True, (
                f"{table_name}.{column.name} is DateTime(timezone=False) — "
                f"ADR-004 rule D2 requires timezone=True on every timestamp column"
            )


@pytest.mark.contract
def test_no_column_is_named_like_a_naive_timestamp_string_field() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name.endswith("_at_str"):
                pytest.fail(f"{table.name}.{column.name} looks like a stringified timestamp")


# --------------------------------------------------------------------------------------
# D3 — structured columns are the generic JSON type, never a dialect-specific JSONB.
# --------------------------------------------------------------------------------------

_EXPECTED_JSON_COLUMNS = {
    "registry_snapshots": {"document"},
    "workflow_bindings": {"input_schema"},
    "operations": {"arguments"},
    "operation_events": {"detail"},
    "execution_results": {"redacted_payload", "node_trace", "error"},
    "audit_log": {"detail"},
}


@pytest.mark.contract
def test_json_columns_use_the_generic_json_type() -> None:
    for table_name, expected_columns in _EXPECTED_JSON_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        for column_name in expected_columns:
            column = table.columns[column_name]
            assert type(column.type) is JSON, (  # exact type, not a dialect subclass
                f"{table_name}.{column_name} is {type(column.type).__name__}, "
                f"not the generic sqlalchemy.JSON type (ADR-004 rule D3)"
            )


@pytest.mark.contract
def test_no_json_column_exists_outside_the_expected_set() -> None:
    actual: dict[str, set[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        json_cols = {c.name for c in table.columns if isinstance(c.type, JSON)}
        if json_cols:
            actual[table_name] = json_cols
    assert actual == _EXPECTED_JSON_COLUMNS


# --------------------------------------------------------------------------------------
# D8 — uniqueness is a database constraint, never left to an application-level check.
# --------------------------------------------------------------------------------------


@pytest.mark.contract
def test_idempotency_namespace_uniqueness_is_a_database_constraint() -> None:
    operations = Base.metadata.tables["operations"]
    unique_constraints = [c.columns.keys() for c in operations.constraints if hasattr(c, "columns")]
    target = {"principal_id", "environment", "workflow_id", "idempotency_key"}
    matches = [cols for cols in unique_constraints if set(cols) == target]
    assert matches, (
        "no UniqueConstraint on (principal_id, environment, workflow_id, idempotency_key)"
    )


@pytest.mark.contract
def test_the_idempotency_constraint_is_not_a_partial_index() -> None:
    """ADR-004's own Consequences section names partial indexes as something v1
    deliberately forgoes. The NULL-uniqueness behavior ADR-011 relies on comes from
    plain SQL semantics, not a WHERE-qualified index — see storage/models.py."""
    operations = Base.metadata.tables["operations"]
    for index in operations.indexes:
        assert "sqlite_where" not in index.dialect_options["sqlite"]._non_defaults
        assert "postgresql_where" not in index.dialect_options["postgresql"]._non_defaults


@pytest.mark.contract
@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("registry_snapshots", "content_hash"),
        ("approvals", "token_hash"),
        ("audit_log", "entry_hash"),
    ],
)
def test_expected_unique_columns_are_database_unique(table_name: str, column_name: str) -> None:
    table = Base.metadata.tables[table_name]
    assert table.columns[column_name].unique is True


# --------------------------------------------------------------------------------------
# D10 — enum-like columns carry a CHECK constraint, not a native database enum type.
# --------------------------------------------------------------------------------------

_EXPECTED_CHECK_CONSTRAINED_COLUMNS = {
    ("principals", "kind"),
    ("workflow_bindings", "side_effects"),
    ("workflow_bindings", "approval_policy"),
    ("operations", "state"),
    ("operation_events", "transition"),
    ("approvals", "decision"),
    ("execution_results", "status"),
    ("audit_log", "outcome"),
}


@pytest.mark.contract
def test_enum_like_columns_have_a_check_constraint() -> None:
    """Matches a CHECK constraint's text against ``<column> IN (`` (optionally preceded
    by ``<column> IS NULL OR``) at the *start* of the constraint text — not merely
    "the column name appears somewhere in the text", which would also match a column
    whose enum *values* happen to spell another column's name (``execution_results``
    has both a ``status`` column constrained to include the value ``'error'`` and an
    unrelated, unconstrained ``error`` column — a substring search conflates the two)."""
    found: set[tuple[str, str]] = set()
    for table_name, table in Base.metadata.tables.items():
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            sqltext = str(constraint.sqltext).strip()
            for column in table.columns:
                name = re.escape(column.name)
                pattern = rf"^(?:{name}\s+IS\s+NULL\s+OR\s+)?{name}\s+IN\s*\("
                if re.match(pattern, sqltext, re.IGNORECASE):
                    found.add((table_name, column.name))
    assert found == _EXPECTED_CHECK_CONSTRAINED_COLUMNS


@pytest.mark.contract
def test_no_column_uses_a_native_enum_type() -> None:
    from sqlalchemy import Enum as SQLEnum

    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, SQLEnum), (
                f"{table.name}.{column.name} uses a native Enum type (ADR-004 rule D10 "
                f"requires a CHECK-constrained text column instead)"
            )


# --------------------------------------------------------------------------------------
# D4 — no engine-specific SQL. Searched for in real string literals only (docstrings
# quoting these constructs to document the rule are excluded — see the module docstring).
# --------------------------------------------------------------------------------------

_FORBIDDEN_SQL_PATTERNS = {
    "INSERT OR REPLACE": re.compile(r"INSERT\s+OR\s+REPLACE", re.IGNORECASE),
    "ON CONFLICT": re.compile(r"\bON\s+CONFLICT\b", re.IGNORECASE),
    "a RETURNING clause": re.compile(r"\bRETURNING\b", re.IGNORECASE),
}


@pytest.mark.contract
@pytest.mark.parametrize("label", sorted(_FORBIDDEN_SQL_PATTERNS.keys()))
def test_no_engine_specific_sql_in_the_orm_or_repository_layer(label: str) -> None:
    pattern = _FORBIDDEN_SQL_PATTERNS[label]
    for path in (STORAGE / "models.py", STORAGE / "repository.py"):
        for literal in _non_docstring_string_literals(path):
            assert not pattern.search(literal), f"{path.name} contains {label} in {literal!r}"


@pytest.mark.contract
def test_session_py_is_the_only_place_pragma_is_issued() -> None:
    """PRAGMA statements are legitimate only at connection setup (ADR-004 D9)."""
    pragma_pattern = re.compile(r"^PRAGMA\s", re.IGNORECASE)
    exempt = {STORAGE / "session.py"}
    for path in STORAGE.rglob("*.py"):
        if path in exempt or VERSIONS_DIR in path.parents:
            continue
        for literal in _non_docstring_string_literals(path):
            assert not pragma_pattern.match(literal.strip()), (
                f"{path.relative_to(REPO_ROOT)} issues a PRAGMA outside session.py"
            )


# --------------------------------------------------------------------------------------
# D5 — all access goes through the SQLAlchemy ORM or Core; no raw SQL string.
# --------------------------------------------------------------------------------------


@pytest.mark.contract
def test_repository_and_models_never_import_sqlalchemys_raw_text_construct() -> None:
    """``sqlalchemy.text()`` is the sanctioned way raw SQL enters a SQLAlchemy codebase
    at all — its absence from these two files' imports is a strong, precise proxy for
    "no raw SQL string is ever executed here", without the false positives a keyword
    search over string literals produces against ordinary English docstring prose."""
    for path in (STORAGE / "models.py", STORAGE / "repository.py"):
        assert not _imports_name_from_sqlalchemy(path, "text"), (
            f"{path.name} imports sqlalchemy.text — D5 requires ORM/Core only"
        )


# --------------------------------------------------------------------------------------
# D6 — the schema is created by migrations; create_all() belongs only in tests.
# --------------------------------------------------------------------------------------


@pytest.mark.contract
def test_create_all_is_never_called_inside_the_shipped_package() -> None:
    """AST-based, not a text search: this module's own docstrings, and env.py's, quote
    ``create_all`` in prose explaining the rule, which a substring search would
    misreport as a violation of the very rule it is documenting."""
    src = REPO_ROOT / "src" / "n8n_operator"
    for path in src.rglob("*.py"):
        assert not _calls_named(path, "create_all"), (
            f"{path.relative_to(REPO_ROOT)} calls create_all"
        )


# --------------------------------------------------------------------------------------
# Migration file itself
# --------------------------------------------------------------------------------------


@pytest.mark.contract
def test_migration_0001_exists_and_is_named_as_the_task_specifies() -> None:
    assert (VERSIONS_DIR / "0001_initial.py").is_file()


@pytest.mark.contract
def test_migration_0001_contains_no_forbidden_constructs() -> None:
    path = VERSIONS_DIR / "0001_initial.py"
    for literal in _non_docstring_string_literals(path):
        assert "JSONB" not in literal
        assert "postgresql_where" not in literal
        assert "sqlite_where" not in literal
        assert "AUTOINCREMENT" not in literal
        assert not re.search(r"\bRETURNING\b", literal, re.IGNORECASE)
    assert not _calls_named(path, "create_all")


@pytest.mark.contract
def test_migration_0001_creates_every_table_and_nothing_else() -> None:
    text = (VERSIONS_DIR / "0001_initial.py").read_text(encoding="utf-8")
    created = set(re.findall(r"op\.create_table\(\s*[\"'](\w+)[\"']", text))
    assert created == set(Base.metadata.tables.keys())


@pytest.mark.contract
def test_audit_log_entry_uses_the_documented_integer_pk_exception_deliberately() -> None:
    """A meta-check on the exception itself: it must be named and explained, not silent."""
    docstring = AuditLogEntry.__doc__ or ""
    assert "D1" in docstring
    assert "exception" in docstring.lower()
