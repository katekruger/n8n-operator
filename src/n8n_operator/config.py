"""Process configuration.

Pydantic v2 ``BaseSettings`` with the ``N8N_OPERATOR_`` prefix, validated at process
start so a malformed configuration is a startup failure rather than a runtime surprise.
Settings are enumerated normatively in ``docs/ARCHITECTURE.md`` section 7.

Credentials are resolved here from the environment or the OS keyring, held in memory,
and never written to the registry, the database, or logs (ADR-006). ``n8n_api_key``
accepts the same ``env:NAME`` / ``keyring:SERVICE/ACCOUNT`` indirection the registry's
``trigger.secret_ref`` uses, resolved once at construction time via
:func:`resolve_secret_reference` and held only as a Pydantic ``SecretStr`` from then on.

:func:`load_settings` is the sanctioned construction path. It is the only place a
Pydantic ``ValidationError`` — whose ``exc.errors()`` entries carry an ``input`` field
that can itself echo back an invalid raw value — is caught and translated into a
:class:`~n8n_operator.errors.ConfigurationError` built only from field locations and
messages, never from that ``input`` field. Constructing :class:`Settings` directly is
for tests that want a validated instance without needing environment variables; nothing
in the codebase's own runtime path should call it instead of :func:`load_settings`.

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, HttpUrl, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from n8n_operator.errors import ConfigurationError

# Mirrors ARCHITECTURE.md section 7's defaults exactly; also imported by
# storage/migrations/env.py so Alembic can resolve a database URL without constructing
# a full Settings object (which would require N8N_BASE_URL/N8N_API_KEY to be set just
# to run a migration — an orthogonal concern migrations should not depend on).
DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./n8n-operator.db"
DEFAULT_REGISTRY_PATH = "./workflows.yaml"
DEFAULT_APPROVAL_BIND = "127.0.0.1:8765"
DEFAULT_HTTP_BIND = "127.0.0.1:8000"
DEFAULT_MAX_ARGUMENT_BYTES = 262_144  # 256 KiB — ADR-011's server ceiling

# Postgres-only connection-pool and session defaults (ADR-004; stage 01). Meaningless on
# SQLite and never applied there — see storage/session.py's dialect branch.
DEFAULT_DATABASE_POOL_SIZE = 5
DEFAULT_DATABASE_MAX_OVERFLOW = 10
DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS = 30
DEFAULT_DATABASE_POOL_RECYCLE_SECONDS = 1800  # 30 min — outlives most managed-Postgres idle kills
DEFAULT_DATABASE_STATEMENT_TIMEOUT_SECONDS = 30
DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS = 10

_SUPPORTED_DB_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite", "postgresql", "postgresql+psycopg"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_SECRET_REF_PATTERN = re.compile(r"^(env|keyring):(.+)$")


def resolve_secret_reference(value: str) -> str:
    """Resolve an ``env:NAME`` or ``keyring:SERVICE/ACCOUNT`` indirection (ADR-006).

    A value with no recognized prefix is returned unchanged: a literal secret placed
    directly in the process environment is already "indirect" in the sense that matters
    here — it never touches a file this codebase writes, and it is exactly the pattern
    ``.env.example`` documents for ``N8N_OPERATOR_N8N_API_KEY``. This function exists for
    the deployments that want the secret to live somewhere else entirely, mirroring the
    same ``env:`` / ``keyring:`` scheme the registry's ``trigger.secret_ref`` uses (rule
    R6), so operators learn one indirection syntax rather than two.

    Every failure message names the *reference* (an environment variable name, or a
    keyring service/account pair) — never a resolved value. Reference names are not
    secrets; what they point to is.
    """
    match = _SECRET_REF_PATTERN.match(value)
    if match is None:
        return value

    scheme, target = match.group(1), match.group(2)
    if scheme == "env":
        resolved = os.environ.get(target)
        if resolved is None:
            raise ValueError(f"secret reference 'env:{target}' has no such environment variable")
        return resolved

    # scheme == "keyring"
    if "/" not in target:
        raise ValueError(f"secret reference 'keyring:{target}' must be 'keyring:SERVICE/ACCOUNT'")
    service, _, account = target.partition("/")
    try:
        import keyring
    except ImportError as exc:
        raise ValueError(
            f"secret reference 'keyring:{target}' requires the optional 'keyring' extra: "
            "install with `uv sync --extra keyring`"
        ) from exc
    resolved = keyring.get_password(service, account)
    if resolved is None:
        raise ValueError(f"secret reference 'keyring:{target}' returned no value")
    return resolved


def _is_loopback(bind: str) -> bool:
    host = bind.rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def _check_database_url(value: str) -> str:
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError(f"database_url is not a valid SQLAlchemy URL ({exc})") from exc
    if url.drivername not in _SUPPORTED_DB_DRIVERS:
        raise ValueError(
            f"database_url driver {url.drivername!r} is not supported; "
            f"use one of {sorted(_SUPPORTED_DB_DRIVERS)}"
        )
    return value


def resolve_database_url(explicit: str | None = None) -> str:
    """The database URL, resolved and validated *without* requiring the rest of
    :class:`Settings` to be present.

    Managing the schema is an orthogonal concern from the rest of the application's
    configuration: an operator should be able to run ``n8n-operator db init`` on a fresh
    clone before ``N8N_OPERATOR_N8N_BASE_URL``/``N8N_OPERATOR_N8N_API_KEY`` are set at
    all. This is the single source of truth both ``storage/migrations/env.py`` and the
    ``db`` CLI commands use, so the two never disagree about precedence or validation:
    an explicit value (``-x db_url=`` for Alembic) wins, then the environment variable,
    then :data:`DEFAULT_DATABASE_URL` — the same default :attr:`Settings.database_url`
    carries. Raises :class:`ValueError` on an unparseable URL or an unsupported driver,
    exactly as the :class:`Settings` field validator does.
    """
    raw = explicit or os.environ.get("N8N_OPERATOR_DATABASE_URL", DEFAULT_DATABASE_URL)
    return _check_database_url(raw)


def resolve_v2_identity_flags() -> tuple[bool, str]:
    """``(enable_v2, dev_principal_id)``, resolved and validated *without* requiring
    the rest of :class:`Settings` (notably ``n8n_base_url``/``n8n_api_key``, both
    required fields) to be present — the same "schema/identity management is
    orthogonal to n8n configuration" reasoning :func:`resolve_database_url` already
    documents, extended to Stage 03's CLI identity resolution
    (``core.identity.resolve_cli_principal_id``) so ``operations``/``audit`` commands
    keep working without n8n configured, exactly as they always have.

    Reads the same env vars :class:`Settings`' own fields do
    (``N8N_OPERATOR_ENABLE_V2``, ``N8N_OPERATOR_DEV_PRINCIPAL_ID``) with the same
    defaults, so a deployment's ``.env``/environment never has to agree with itself
    twice.
    """
    raw_enable_v2 = os.environ.get("N8N_OPERATOR_ENABLE_V2", "false").strip().lower()
    enable_v2 = raw_enable_v2 in {"1", "true", "yes", "on"}
    dev_principal_id = os.environ.get("N8N_OPERATOR_DEV_PRINCIPAL_ID", "dev")
    return enable_v2, dev_principal_id


def redact_database_url(database_url: str) -> str:
    """``database_url`` with any embedded password replaced by SQLAlchemy's own
    ``***`` placeholder — never the literal value.

    A PostgreSQL DSN may carry a password inline (``postgresql+psycopg://user:pass@host/db``).
    Every place that surfaces ``database_url`` to a human or a log line — ``db status``,
    ``db migrate-to-postgres``, the health check, structured logging — must call this
    first. Malformed input returns a fixed placeholder rather than raising, because the
    only callers of this function are already-successful display paths for a URL that
    validated earlier; failing to redact must never be the reason a raw secret reaches
    a log line.
    """
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        return "***"


def resolve_database_password(explicit: str | None = None) -> str | None:
    """The database password, resolved *without* requiring the rest of
    :class:`Settings` — the same "orthogonal concern" reasoning as
    :func:`resolve_database_url`. Precedence: an explicit value, then
    ``N8N_OPERATOR_DATABASE_PASSWORD``, then ``None`` (the ``database_url`` itself may
    already carry a password, or none is needed — e.g. local SQLite). Accepts the same
    ``env:NAME`` / ``keyring:SERVICE/ACCOUNT`` indirection as ``n8n_api_key`` (ADR-006).
    """
    raw = explicit if explicit is not None else os.environ.get("N8N_OPERATOR_DATABASE_PASSWORD")
    if raw is None:
        return None
    return resolve_secret_reference(raw)


def compose_database_url(database_url: str, password: str | None) -> str:
    """``database_url`` with ``password`` substituted as its password component.

    A no-op when ``password`` is ``None`` — the URL is returned exactly as given, which
    covers both "no password needed" (SQLite) and "the password is already embedded in
    the URL" (an operator who prefers that shape). The composed, secret-bearing string
    this returns must never be logged or displayed — use :func:`redact_database_url` for
    that; this function exists only to build the value actually passed to
    ``create_engine``.
    """
    if password is None:
        return database_url
    return make_url(database_url).set(password=password).render_as_string(hide_password=False)


def resolve_registry_path(explicit: str | None = None) -> Path:
    """The registry file path, resolved *without* requiring the rest of
    :class:`Settings` — the same "schema/registry management is orthogonal to the rest
    of configuration" reasoning as :func:`resolve_database_url`. Precedence: an explicit
    value, then ``N8N_OPERATOR_REGISTRY_PATH``, then :data:`DEFAULT_REGISTRY_PATH`.
    """
    raw = explicit or os.environ.get("N8N_OPERATOR_REGISTRY_PATH", DEFAULT_REGISTRY_PATH)
    return Path(raw)


def resolve_approval_bind(explicit: str | None = None) -> str:
    """The approval app's bind address, resolved *without* requiring the rest of
    :class:`Settings` — the same "orthogonal concern" reasoning as
    :func:`resolve_database_url`, applied here so ``n8n-operator serve approval`` and
    the ``operations`` CLI commands never need ``N8N_OPERATOR_N8N_BASE_URL``/
    ``N8N_OPERATOR_N8N_API_KEY`` just to approve or reject an operation — neither
    touches n8n. Precedence: an explicit value, then ``N8N_OPERATOR_APPROVAL_BIND``,
    then :data:`DEFAULT_APPROVAL_BIND`.

    Raises :class:`ValueError` if the resolved bind is not a loopback address —
    boundary B10 admits no exception, so this is checked here too, not only inside
    :class:`Settings`' own validator.
    """
    raw = explicit or os.environ.get("N8N_OPERATOR_APPROVAL_BIND", DEFAULT_APPROVAL_BIND)
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"{raw!r} is not a HOST:PORT address")
    if not _is_loopback(raw):
        raise ValueError(
            f"approval_bind must be a loopback address in v1 (boundary B10); got {raw!r}"
        )
    return raw


def resolve_max_argument_bytes(explicit: int | None = None) -> int:
    """The server argument-size ceiling (ADR-011), resolved *without* requiring the
    rest of :class:`Settings`. Precedence: an explicit value, then
    ``N8N_OPERATOR_MAX_ARGUMENT_BYTES``, then :data:`DEFAULT_MAX_ARGUMENT_BYTES`.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("N8N_OPERATOR_MAX_ARGUMENT_BYTES")
    if raw is None:
        return DEFAULT_MAX_ARGUMENT_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"N8N_OPERATOR_MAX_ARGUMENT_BYTES={raw!r} is not an integer") from exc
    if value <= 0:
        raise ValueError(f"N8N_OPERATOR_MAX_ARGUMENT_BYTES must be positive, got {value}")
    return value


class Settings(BaseSettings):
    """Validated process configuration (ARCHITECTURE section 7).

    Every field here is either required with no default (the two n8n credentials — a
    missing value must fail startup, never fall back silently) or carries the exact
    default ARCHITECTURE.md publishes. Construct via :func:`load_settings`, not directly.
    """

    model_config = SettingsConfigDict(
        env_prefix="N8N_OPERATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- n8n instance ----------------------------------------------------------------
    n8n_base_url: HttpUrl = Field(
        ..., description="Required. Never returned by any tool (ADR-006)."
    )
    n8n_api_key: SecretStr = Field(
        ..., description="Required. Literal, env:NAME, or keyring:SERVICE/ACCOUNT (ADR-006)."
    )

    # --- registry ----------------------------------------------------------------------
    registry_path: Path = Field(default=Path(DEFAULT_REGISTRY_PATH))

    # --- storage -------------------------------------------------------------------------
    database_url: str = Field(default=DEFAULT_DATABASE_URL)
    database_password: SecretStr | None = Field(
        default=None,
        description=(
            "Optional. Literal, env:NAME, or keyring:SERVICE/ACCOUNT (ADR-006). "
            "Substituted into database_url's password component; never logged."
        ),
    )
    database_pool_size: int = Field(default=DEFAULT_DATABASE_POOL_SIZE, ge=1)
    database_max_overflow: int = Field(default=DEFAULT_DATABASE_MAX_OVERFLOW, ge=0)
    database_pool_timeout_seconds: int = Field(default=DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS, gt=0)
    database_pool_recycle_seconds: int = Field(default=DEFAULT_DATABASE_POOL_RECYCLE_SECONDS, gt=0)
    database_statement_timeout_seconds: int = Field(
        default=DEFAULT_DATABASE_STATEMENT_TIMEOUT_SECONDS, gt=0
    )
    database_connect_timeout_seconds: int = Field(
        default=DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS, gt=0
    )

    # --- approval (loopback only in v1 — boundary B10) ------------------------------------
    approval_bind: str = Field(default=DEFAULT_APPROVAL_BIND)

    # --- MCP Streamable HTTP transport ----------------------------------------------------
    http_bind: str = Field(default=DEFAULT_HTTP_BIND)
    http_bearer_token: SecretStr | None = Field(default=None)
    http_allowed_origins: str = Field(
        default="", description="Comma-separated. Use allowed_origins() to read it."
    )

    # --- behavior --------------------------------------------------------------------------
    request_timeout_seconds: int = Field(default=60, gt=0)
    max_argument_bytes: int = Field(default=DEFAULT_MAX_ARGUMENT_BYTES, gt=0)  # ADR-011, B12
    approval_url_exposure: Literal["auto", "never"] = Field(default="auto")  # ADR-010, I12
    log_level: str = Field(default="INFO")

    # --- v2 identity (ADR-013, ADR-014; stage 02) -------------------------------------------
    # Default off: v1's exact 12-tool surface (AC-23) is unaffected unless an operator
    # deliberately opts in. When True, `whoami` registers as a 13th tool and identity_mode
    # governs how every other tool call's caller is resolved.
    enable_v2: bool = Field(default=False)
    # "dev": every caller (stdio, and non-OIDC HTTP) is attributed to one fixed, clearly
    # labeled, non-production service principal — v1's ergonomics, a real v2 identity row
    # instead of a hardcoded exemption (ADR-014 section 5). "oidc": Streamable HTTP callers
    # authenticate with a real bearer JWT against oidc_issuer_url/oidc_audience; stdio still
    # uses the fixed service principal regardless (ADR-014 section 5 — no OIDC over stdio).
    identity_mode: Literal["dev", "oidc"] = Field(default="dev")
    oidc_issuer_url: HttpUrl | None = Field(default=None)
    oidc_audience: str | None = Field(default=None)
    # Optional: skips OIDC discovery when the provider's discovery document is unavailable
    # or nonstandard. Most deployments should leave this unset.
    oidc_jwks_uri: HttpUrl | None = Field(default=None)
    # Operator's own externally-reachable URL — optional, used only to populate the
    # WWW-Authenticate header's resource_metadata hint (RFC 9728) on a 401. Distinct
    # from oidc_audience (what the JWT's `aud` claim must equal, which need not be a
    # URL at all); leaving this unset only omits that one hint, never bearer-auth
    # enforcement itself.
    oidc_resource_server_url: HttpUrl | None = Field(default=None)
    dev_principal_id: str = Field(default="dev")

    # ---- field-level validation -------------------------------------------------------

    @field_validator("n8n_api_key", mode="before")
    @classmethod
    def _resolve_n8n_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            return resolve_secret_reference(value)
        return value

    @field_validator("database_password", mode="before")
    @classmethod
    def _resolve_database_password(cls, value: object) -> object:
        if isinstance(value, str):
            return resolve_secret_reference(value)
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_LOG_LEVELS)}, got {value!r}")
        return upper

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        return _check_database_url(value)

    @field_validator("approval_bind", "http_bind")
    @classmethod
    def _validate_bind_shape(cls, value: str) -> str:
        host, _, port = value.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"{value!r} is not a HOST:PORT address")
        return value

    # ---- cross-field validation ---------------------------------------------------------

    @model_validator(mode="after")
    def _validate_approval_bind_is_loopback(self) -> Settings:
        # Boundary B10: never configurable to a public interface in v1, no exceptions.
        if not _is_loopback(self.approval_bind):
            raise ValueError(
                f"approval_bind must be a loopback address in v1 (boundary B10); "
                f"got {self.approval_bind!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_http_bind_guard(self) -> Settings:
        # Boundary B9: a non-loopback MCP HTTP bind always requires an Origin
        # allowlist (DNS-rebinding defense, independent of how identity is proven), or
        # startup must fail — never a silently-open listener. It also requires a
        # caller-authentication mechanism: v1's static bearer token, unless v2 OIDC
        # identity is configured, which *replaces* — not layers onto — the static
        # token (ADR-014 section 1). A real bearer JWT per request is what actually
        # authenticates each caller once OIDC is active; the static token would be
        # dead configuration alongside it.
        if _is_loopback(self.http_bind):
            return self
        if not self.allowed_origins():
            raise ValueError(
                "http_bind is non-loopback; an Origin allowlist is required, or "
                "startup must fail (boundary B9)"
            )
        oidc_configured = self.enable_v2 and self.identity_mode == "oidc"
        if self.http_bearer_token is None and not oidc_configured:
            raise ValueError(
                "http_bind is non-loopback; a bearer token is required unless v2 OIDC "
                "identity is configured (identity_mode='oidc'), or startup must fail "
                "(boundary B9, ADR-014 section 1)"
            )
        return self

    @model_validator(mode="after")
    def _validate_v2_identity_mode(self) -> Settings:
        if not self.enable_v2:
            return self
        if self.identity_mode == "dev" and not _is_loopback(self.http_bind):
            # A caller attributed to one shared, unauthenticated dev principal must
            # never be reachable beyond the local machine — "dev" is unsafe non-loopback
            # by construction, not by an operator remembering to keep it local.
            raise ValueError(
                "identity_mode is 'dev'; http_bind must stay loopback. Configure "
                "identity_mode='oidc' with oidc_issuer_url/oidc_audience for a "
                "non-loopback v2 deployment"
            )
        if self.identity_mode == "oidc" and (
            self.oidc_issuer_url is None or not self.oidc_audience
        ):
            raise ValueError(
                "identity_mode is 'oidc'; oidc_issuer_url and oidc_audience are both required"
            )
        return self

    # ---- derived accessors ---------------------------------------------------------------

    def allowed_origins(self) -> tuple[str, ...]:
        """``http_allowed_origins`` split, stripped, and emptied of blanks."""
        return tuple(o.strip() for o in self.http_allowed_origins.split(",") if o.strip())

    def approval_bind_host_port(self) -> tuple[str, int]:
        host, _, port = self.approval_bind.rpartition(":")
        return host, int(port)

    def http_bind_host_port(self) -> tuple[str, int]:
        host, _, port = self.http_bind.rpartition(":")
        return host, int(port)

    def effective_database_url(self) -> str:
        """``database_url`` with ``database_password`` (if set) substituted in.

        The one value ``storage.session.create_engine_for_url`` should actually be
        called with. Never log or display this — it may carry the resolved secret; use
        :func:`redact_database_url` for display, exactly as ``database_url`` itself
        would be redacted.
        """
        password = self.database_password.get_secret_value() if self.database_password else None
        return compose_database_url(self.database_url, password)


def load_settings(**overrides: Any) -> Settings:
    """Construct :class:`Settings`, translating a validation failure safely.

    ``overrides`` is typed ``Any`` deliberately: callers pass a mix of field values
    (``n8n_base_url=...``) and pydantic-settings' own special constructor parameters
    (``_env_file=None``, used by tests to isolate against the ambient environment), and
    no single static type describes that union. Pydantic validates every field at
    runtime regardless of what mypy could check here.

    A Pydantic ``ValidationError``'s ``exc.errors()`` entries carry an ``input`` key that
    can echo back the raw invalid value verbatim — for ``n8n_api_key`` that value may be
    an unresolved secret. This function builds its :class:`ConfigurationError` message
    from only each error's field location (``loc``) and its message (``msg``); it never
    touches ``input``. This is the one place that boundary is enforced, which is exactly
    why every runtime code path must construct settings through here and not by calling
    ``Settings(...)`` directly.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        summary = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigurationError(
            f"invalid configuration: {summary}",
            details={"error_count": exc.error_count()},
        ) from None


__all__ = [
    "DEFAULT_APPROVAL_BIND",
    "DEFAULT_DATABASE_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_DATABASE_MAX_OVERFLOW",
    "DEFAULT_DATABASE_POOL_RECYCLE_SECONDS",
    "DEFAULT_DATABASE_POOL_SIZE",
    "DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS",
    "DEFAULT_DATABASE_STATEMENT_TIMEOUT_SECONDS",
    "DEFAULT_DATABASE_URL",
    "DEFAULT_HTTP_BIND",
    "DEFAULT_MAX_ARGUMENT_BYTES",
    "DEFAULT_REGISTRY_PATH",
    "Settings",
    "compose_database_url",
    "load_settings",
    "redact_database_url",
    "resolve_approval_bind",
    "resolve_database_password",
    "resolve_database_url",
    "resolve_max_argument_bytes",
    "resolve_registry_path",
    "resolve_secret_reference",
    "resolve_v2_identity_flags",
]
