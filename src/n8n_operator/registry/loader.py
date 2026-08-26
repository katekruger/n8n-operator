"""Parse, validate, canonicalize, and hash the registry.

Loading is all-or-nothing: any violation of rules R1-R12 (BUILD_PLAN section 6.6) fails
the load. There is no partially-live allowlist (AC-02). :func:`load_registry` is the
single entry point; every step before it (:func:`parse_registry_yaml`,
:func:`resolve_document`, :func:`check_rules`) is exposed separately for testing and for
``registry validate``'s "report every problem in one pass" behavior.

**This module never touches storage.** ARCHITECTURE.md section 2.1 places ``registry/``
in the Capability layer, which "must not depend on each other or on core/" — turning a
successful :class:`LoadedRegistry` into a persisted ``RegistrySnapshot`` and its
``WorkflowBinding`` rows is ``core/service.py``'s job (Phase 3, with a phase-2 slice
already present for ``reload_registry``), since only ``core/`` is permitted to depend on
both ``registry/`` and ``storage/`` at once.

:func:`canonical_json_bytes` is the one canonical-JSON implementation this codebase
uses everywhere a stable hash over JSON-shaped data is needed. ``core/idempotency.py``
imports it from here for argument fingerprinting (ADR-011) rather than reimplementing
the same algorithm a second time — ``core`` depending on ``registry`` is the sanctioned
direction (BUILD_PLAN section 4: ``core -> registry, storage, audit, n8n``).

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from pydantic import ValidationError as PydanticValidationError

from n8n_operator.errors import RegistryUnavailableError
from n8n_operator.registry.schema import (
    RegistryDocument,
    WorkflowEntry,
    resolve_workflow_entry,
)

# --------------------------------------------------------------------------------------
# Strict, safe YAML parsing
# --------------------------------------------------------------------------------------

MAX_REGISTRY_FILE_BYTES = 2 * 1024 * 1024
"""A hand-authored registry file has no business exceeding this. Distinct from, and
unrelated to, the per-*call-argument* ceiling ``N8N_OPERATOR_MAX_ARGUMENT_BYTES``
governs (ADR-011) — this bounds the YAML *file*, checked before it is even read."""

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")  # BUILD_PLAN section 6.2
DEFINITION_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")  # rule R7
SECRET_REF_PATTERN = re.compile(r"^(env|keyring):.+$")  # rule R6
SUPPORTED_API_VERSIONS = frozenset({"n8n-operator/v1"})  # rule R1


class _StrictSafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` — never constructs an arbitrary Python object, only plain
    scalars/lists/mappings — with duplicate mapping keys additionally rejected.

    PyYAML's own ``SafeLoader`` silently keeps the *last* value when a mapping key is
    repeated; a registry is meant to be reviewed in a diff, and a duplicate key (most
    often a copy-paste mistake editing one workflow entry into another) is exactly the
    kind of error that should fail loudly rather than silently picking a winner.
    """


def _construct_mapping_rejecting_duplicates(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_rejecting_duplicates
)


class RegistryParseError(RegistryUnavailableError):
    """The registry file could not even be parsed into a document: unreadable, too
    large, invalid YAML, or a duplicate key. Distinct from
    :class:`RegistryValidationError`, which is for a document that parsed cleanly but
    fails one or more load-time rules."""

    default_message = "The registry file could not be parsed."


def read_registry_source(path: Path) -> str:
    """Read ``path`` as UTF-8 text, enforcing :data:`MAX_REGISTRY_FILE_BYTES` before a
    single byte is parsed."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RegistryParseError(
            f"cannot read registry file: {path}", details={"path": str(path)}
        ) from exc
    if size > MAX_REGISTRY_FILE_BYTES:
        raise RegistryParseError(
            f"registry file exceeds the {MAX_REGISTRY_FILE_BYTES}-byte limit",
            details={"path": str(path), "size": size, "limit": MAX_REGISTRY_FILE_BYTES},
        )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryParseError(
            f"cannot read registry file: {path}", details={"path": str(path)}
        ) from exc
    except UnicodeDecodeError as exc:
        raise RegistryParseError(
            "registry file is not valid UTF-8", details={"path": str(path)}
        ) from exc


def parse_registry_yaml(source: str) -> Any:
    """Parse ``source`` with the strict, safe loader. Never constructs an arbitrary
    object and never silently accepts a duplicate mapping key."""
    try:
        # _StrictSafeLoader subclasses yaml.SafeLoader (never constructs an arbitrary
        # Python object) and additionally rejects duplicate mapping keys — the ruff
        # bandit rule below only pattern-matches on "not yaml.SafeLoader literally".
        return yaml.load(source, Loader=_StrictSafeLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise RegistryParseError(f"invalid YAML: {exc}") from exc


# --------------------------------------------------------------------------------------
# Canonical JSON — the one implementation this codebase uses everywhere (see the module
# docstring). Not a claim of full RFC 8785 (JCS) compliance: the concrete guarantee is
# "the same Python value, of the types this codebase actually produces (str, int, bool,
# float, None, list, dict), always serializes to the same bytes" — sufficient for a
# content hash and for an argument fingerprint, which is all either caller needs.
# --------------------------------------------------------------------------------------


def _nfc_normalize(value: Any) -> Any:
    """Recursively NFC-normalize every string leaf (CAN-04-style determinism)."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc_normalize(k): _nfc_normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc_normalize(v) for v in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """A deterministic JSON serialization of ``value``, as UTF-8 bytes.

    Keys sorted by code point (``sort_keys=True``, which sorts ``str`` keys by Unicode
    code point), array order preserved (JSON arrays are already ordered), every string
    NFC-normalized, no insignificant whitespace (compact separators), UTF-8 throughout.
    ``allow_nan=False`` rejects ``NaN``/``Infinity`` rather than silently emitting
    invalid JSON for them. Idempotent: canonicalizing an already-canonical structure
    reproduces the same bytes.
    """
    normalized = _nfc_normalize(value)
    text = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------------
# Load-time rules R1-R12 (BUILD_PLAN section 6.6)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleViolation:
    """One failed check, named so an operator can act on it directly.

    ``rule`` is ``"R1"``..``"R12"`` for the numbered rules, or ``"SCHEMA"`` for a
    structural (Pydantic) failure that isn't one of the twelve — a missing required
    field or a wrong type, which is not itself a semantic policy rule.
    """

    rule: str
    message: str
    workflow_id: str | None = None

    def format(self) -> str:
        location = f" ({self.workflow_id})" if self.workflow_id else ""
        return f"{self.rule}{location}: {self.message}"


class RegistryValidationError(RegistryUnavailableError):
    """One or more load-time rules were violated. Carries every violation found in a
    single pass — ``registry validate`` reports them all at once — not just the first;
    the registry still fails to load as a whole regardless of how many rules failed
    (BUILD_PLAN section 6.6: no partially-live allowlist)."""

    def __init__(self, violations: list[RuleViolation]) -> None:
        self.violations = violations
        summary = "; ".join(v.format() for v in violations)
        super().__init__(
            f"registry failed validation ({len(violations)} problem(s)): {summary}",
            details={"violations": [v.format() for v in violations]},
        )


def _check_r1_api_version(document: RegistryDocument) -> RuleViolation | None:
    if document.api_version not in SUPPORTED_API_VERSIONS:
        return RuleViolation(
            "R1",
            f"apiVersion {document.api_version!r} is not supported; "
            f"use one of {sorted(SUPPORTED_API_VERSIONS)}",
        )
    return None


def _check_r2_id(entries: list[WorkflowEntry]) -> list[RuleViolation]:
    violations = []
    seen: set[str] = set()
    for entry in entries:
        if not ID_PATTERN.match(entry.id):
            violations.append(
                RuleViolation(
                    "R2", f"id {entry.id!r} does not match {ID_PATTERN.pattern}", entry.id
                )
            )
        if entry.id in seen:
            violations.append(RuleViolation("R2", f"duplicate id {entry.id!r}", entry.id))
        seen.add(entry.id)
    return violations


def _check_r3_n8n_workflow_id(entries: list[WorkflowEntry]) -> list[RuleViolation]:
    violations = []
    seen: dict[str, str] = {}
    for entry in entries:
        prior = seen.get(entry.n8n_workflow_id)
        if prior is not None:
            violations.append(
                RuleViolation(
                    "R3",
                    f"n8n_workflow_id {entry.n8n_workflow_id!r} is also used by {prior!r}",
                    entry.id,
                )
            )
        else:
            seen[entry.n8n_workflow_id] = entry.id
    return violations


def _check_r4_input_schema(entry: WorkflowEntry) -> RuleViolation | None:
    schema = entry.input_schema
    if schema.get("type") != "object":
        return RuleViolation("R4", "input_schema must have type: object", entry.id)
    if schema.get("additionalProperties") is not False:
        return RuleViolation("R4", "input_schema must set additionalProperties: false", entry.id)
    try:
        validator_cls = validator_for(schema, default=Draft202012Validator)
        validator_cls.check_schema(schema)
    except SchemaError as exc:
        return RuleViolation("R4", f"input_schema is not a valid schema: {exc.message}", entry.id)
    return None


def _check_r5_r10_approval_policy(resolved: WorkflowEntry) -> list[RuleViolation]:
    violations = []
    assert resolved.approval is not None  # resolved entries always have a concrete value
    if resolved.approval == "none" and resolved.side_effects != "read_only":
        violations.append(
            RuleViolation(
                "R5",
                f"approval: none requires side_effects: read_only "
                f"(this entry is {resolved.side_effects!r})",
                resolved.id,
            )
        )
    if resolved.risk == "high" and resolved.approval != "required":
        violations.append(
            RuleViolation(
                "R10",
                "risk: high requires approval: required (defaults may not weaken this)",
                resolved.id,
            )
        )
    return violations


def _check_r6_secret_ref(entry: WorkflowEntry) -> RuleViolation | None:
    trigger = entry.trigger
    if trigger.auth == "none":
        return None
    if trigger.secret_ref is None:
        return RuleViolation(
            "R6", f"trigger.secret_ref is required when auth is {trigger.auth!r}", entry.id
        )
    if not SECRET_REF_PATTERN.match(trigger.secret_ref):
        return RuleViolation(
            "R6",
            "trigger.secret_ref must be an indirect reference (env:NAME or "
            "keyring:SERVICE/ACCOUNT), never a literal secret",
            entry.id,
        )
    return None


def _check_r7_definition_hash(entry: WorkflowEntry) -> RuleViolation | None:
    if not DEFINITION_HASH_PATTERN.match(entry.definition_hash):
        return RuleViolation(
            "R7",
            f"definition_hash {entry.definition_hash!r} is not sha256:<64 hex chars>",
            entry.id,
        )
    return None


def _check_r8_trigger_path(entry: WorkflowEntry) -> RuleViolation | None:
    path = entry.trigger.path
    if "://" in path:
        return RuleViolation("R8", "trigger.path must not be an absolute URL", entry.id)
    if not path.startswith("/"):
        return RuleViolation("R8", "trigger.path must start with '/'", entry.id)
    # A leading "//" is host-relative in URL terms (e.g. "//evil.example/x") — reject it
    # the same way an absolute URL is rejected; it still names a host, just implicitly.
    if path.startswith("//"):
        return RuleViolation("R8", "trigger.path must not contain a host component", entry.id)
    return None


def _check_r9_redact_paths(entry: WorkflowEntry) -> list[RuleViolation]:
    from jsonpath_ng import parse as parse_jsonpath

    violations = []
    for expr in entry.output.redact:
        try:
            parse_jsonpath(expr)
        except Exception as exc:
            violations.append(
                RuleViolation("R9", f"output.redact path {expr!r} does not parse: {exc}", entry.id)
            )
    return violations


def _check_r11_max_argument_bytes(
    entry: WorkflowEntry, *, server_max_argument_bytes: int
) -> RuleViolation | None:
    limit = entry.limits.max_argument_bytes
    if limit is None:
        return None
    if limit <= 0:
        return RuleViolation(
            "R11", "limits.max_argument_bytes must be a positive integer", entry.id
        )
    if limit > server_max_argument_bytes:
        return RuleViolation(
            "R11",
            f"limits.max_argument_bytes ({limit}) exceeds the server ceiling "
            f"({server_max_argument_bytes}); a workflow may only lower it",
            entry.id,
        )
    return None


def _check_r12_correlation(entry: WorkflowEntry) -> RuleViolation | None:
    # trigger.type is Literal["webhook"] in v1 (schema.py), so "response_envelope is
    # only valid for trigger.type: webhook" can never actually fire yet — checked
    # anyway so the rule holds the moment a second trigger type is introduced, and so
    # this function documents the rule rather than silently relying on the schema.
    if entry.trigger.correlation == "response_envelope" and entry.trigger.type != "webhook":
        return RuleViolation(  # type: ignore[unreachable]
            "R12",
            "trigger.correlation: response_envelope is only valid for a webhook trigger",
            entry.id,
        )
    return None


def check_rules(
    document: RegistryDocument,
    resolved_entries: list[WorkflowEntry],
    *,
    server_max_argument_bytes: int,
) -> list[RuleViolation]:
    """Run every load-time rule (R1-R12) and return every violation found.

    ``resolved_entries`` must be ``document.workflows`` with :func:`resolve_workflow_entry`
    already applied to each — R5/R10 are checked against the *effective* policy, not
    against whatever an individual entry happened to leave unset.
    """
    violations: list[RuleViolation] = []

    r1 = _check_r1_api_version(document)
    if r1:
        violations.append(r1)

    violations.extend(_check_r2_id(document.workflows))
    violations.extend(_check_r3_n8n_workflow_id(document.workflows))

    for raw_entry, resolved in zip(document.workflows, resolved_entries, strict=True):
        r4 = _check_r4_input_schema(raw_entry)
        if r4:
            violations.append(r4)
        violations.extend(_check_r5_r10_approval_policy(resolved))
        r6 = _check_r6_secret_ref(raw_entry)
        if r6:
            violations.append(r6)
        r7 = _check_r7_definition_hash(raw_entry)
        if r7:
            violations.append(r7)
        r8 = _check_r8_trigger_path(raw_entry)
        if r8:
            violations.append(r8)
        violations.extend(_check_r9_redact_paths(raw_entry))
        r11 = _check_r11_max_argument_bytes(
            raw_entry, server_max_argument_bytes=server_max_argument_bytes
        )
        if r11:
            violations.append(r11)
        r12 = _check_r12_correlation(raw_entry)
        if r12:
            violations.append(r12)

    return violations


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def parse_registry_document(raw: Any) -> RegistryDocument:
    """Validate a parsed YAML value into a :class:`RegistryDocument`.

    A Pydantic structural failure (missing required field, wrong type, an unknown
    field under ``extra="forbid"``) is wrapped into a :class:`RegistryValidationError`
    carrying one ``RuleViolation`` per Pydantic error, tagged ``"SCHEMA"`` — reported
    through the same unified path as R1-R12, not a separately-shaped exception.
    """
    try:
        return RegistryDocument.model_validate(raw)
    except PydanticValidationError as exc:
        violations = [
            RuleViolation(
                "SCHEMA",
                error["msg"],
                workflow_id=_workflow_id_from_error_location(raw, error["loc"]),
            )
            for error in exc.errors()
        ]
        raise RegistryValidationError(violations) from None


def _workflow_id_from_error_location(raw: Any, loc: tuple[Any, ...]) -> str | None:
    """Best-effort: if a Pydantic error location starts with ``('workflows', N, ...)``,
    resolve it to that entry's own ``id`` (if the raw document has one) for a more
    useful message than a bare list index."""
    if len(loc) >= 2 and loc[0] == "workflows" and isinstance(loc[1], int):
        try:
            candidate = raw["workflows"][loc[1]]
        except (KeyError, IndexError, TypeError):
            return None
        if isinstance(candidate, dict):
            entry_id = candidate.get("id")
            return entry_id if isinstance(entry_id, str) else None
    return None


def resolve_document(document: RegistryDocument) -> list[WorkflowEntry]:
    """Every workflow entry with ``defaults`` merged in (:func:`resolve_workflow_entry`)."""
    return [resolve_workflow_entry(entry, document.defaults) for entry in document.workflows]


@dataclass(frozen=True)
class LoadedRegistry:
    """The pure, storage-agnostic result of a successful load.

    ``document`` is the fully-resolved, canonical-form dict this registry's
    ``content_hash`` is taken over — what a :class:`~n8n_operator.storage.models.RegistrySnapshot`
    row's own ``document`` column stores (BUILD_PLAN section 6.7). ``entries`` is the
    same data as typed, resolved :class:`WorkflowEntry` objects, for callers that want
    to work with them directly rather than re-parsing the dict.
    """

    content_hash: str
    document: dict[str, Any]
    entries: list[WorkflowEntry] = field(repr=False)
    source_path: str
    loaded_at: datetime


def load_registry(path: Path, *, server_max_argument_bytes: int) -> LoadedRegistry:
    """Parse, validate, resolve, canonicalize, and hash the registry at ``path``.

    Raises :class:`RegistryParseError` if the file cannot even be read or parsed as
    YAML, or :class:`RegistryValidationError` carrying every violation found if the
    parsed document fails one or more load-time rules. Returns a :class:`LoadedRegistry`
    only when the registry is entirely clean — there is no partial success.
    """
    source = read_registry_source(path)
    raw = parse_registry_yaml(source)
    document = parse_registry_document(raw)
    resolved_entries = resolve_document(document)

    violations = check_rules(
        document, resolved_entries, server_max_argument_bytes=server_max_argument_bytes
    )
    if violations:
        raise RegistryValidationError(violations)

    canonical_document = _canonical_document(document, resolved_entries)
    content_hash = "sha256:" + sha256_hex(canonical_json_bytes(canonical_document))

    return LoadedRegistry(
        content_hash=content_hash,
        document=canonical_document,
        entries=resolved_entries,
        source_path=str(path),
        loaded_at=datetime.now(UTC),
    )


def _canonical_document(
    document: RegistryDocument, resolved_entries: list[WorkflowEntry]
) -> dict[str, Any]:
    """The dict that gets hashed and stored: the document with every workflow entry
    replaced by its *resolved* form.

    Hashing the resolved form, not the literal YAML, means the hash reflects the
    *effective governed contract* — whether an entry inherited ``approval`` from
    ``defaults`` or spelled it out explicitly, two registries with the same effective
    policy hash identically, which is the property an audit reader actually cares
    about ("which contract was in force"), not literal-text trivia.
    """
    payload = document.model_dump(mode="json", by_alias=True)
    payload["workflows"] = [entry.model_dump(mode="json") for entry in resolved_entries]
    return payload


__all__ = [
    "MAX_REGISTRY_FILE_BYTES",
    "LoadedRegistry",
    "RegistryParseError",
    "RegistryValidationError",
    "RuleViolation",
    "canonical_json_bytes",
    "check_rules",
    "load_registry",
    "parse_registry_document",
    "parse_registry_yaml",
    "read_registry_source",
    "resolve_document",
    "sha256_hex",
]
