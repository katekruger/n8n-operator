"""Configuration validation and secret redaction (BUILD_PLAN section 12, phase 1).

Every test constructs :class:`Settings` with ``_env_file=None`` so the ambient
environment's ``.env`` (if a developer has one, per ``.env.example``) can never leak
into a test's expectations — the whole point of these tests is to control every input
precisely.
"""

from __future__ import annotations

import sys

import pytest
from pydantic import SecretStr

from n8n_operator.config import (
    DEFAULT_APPROVAL_BIND,
    DEFAULT_DATABASE_URL,
    DEFAULT_HTTP_BIND,
    Settings,
    load_settings,
    resolve_database_url,
    resolve_secret_reference,
)
from n8n_operator.errors import ConfigurationError

REQUIRED = {
    "n8n_base_url": "https://n8n.example.com",
    "n8n_api_key": "literal-api-key-value",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})  # type: ignore[call-arg, arg-type]


# --------------------------------------------------------------------------------------
# Construction and defaults
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_minimal_required_fields_construct_successfully() -> None:
    settings = _settings()
    assert str(settings.n8n_base_url) == "https://n8n.example.com/"
    assert settings.n8n_api_key.get_secret_value() == "literal-api-key-value"


@pytest.mark.unit
def test_defaults_match_architecture_md() -> None:
    settings = _settings()
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.approval_bind == DEFAULT_APPROVAL_BIND
    assert settings.http_bind == DEFAULT_HTTP_BIND
    assert settings.request_timeout_seconds == 60
    assert settings.max_argument_bytes == 262_144
    assert settings.approval_url_exposure == "auto"
    assert settings.log_level == "INFO"
    assert settings.http_bearer_token is None
    assert settings.allowed_origins() == ()


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["n8n_base_url", "n8n_api_key"])
def test_missing_required_field_fails_startup(missing: str) -> None:
    fields = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(_env_file=None, **fields)
    assert missing in excinfo.value.message


@pytest.mark.unit
def test_settings_is_frozen() -> None:
    settings = _settings()
    with pytest.raises((TypeError, ValueError)):
        settings.log_level = "DEBUG"


# --------------------------------------------------------------------------------------
# Secret indirection (ADR-006)
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_literal_api_key_used_as_is() -> None:
    assert resolve_secret_reference("plain-value") == "plain-value"


@pytest.mark.unit
def test_env_indirection_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_OTHER_SECRET", "resolved-secret-value")
    assert resolve_secret_reference("env:SOME_OTHER_SECRET") == "resolved-secret-value"


@pytest.mark.unit
def test_env_indirection_missing_variable_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    with pytest.raises(ValueError, match="env:DEFINITELY_NOT_SET") as excinfo:
        resolve_secret_reference("env:DEFINITELY_NOT_SET")
    # The message names the *reference*, never a value — there is nothing else to leak
    # here since the variable was never set, but the error shape is what matters.
    assert "DEFINITELY_NOT_SET" in str(excinfo.value)


@pytest.mark.unit
def test_keyring_indirection_without_the_extra_installed_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 'keyring' package is an optional extra (pyproject.toml) — many operators
    # genuinely won't have it, and the failure must be a clear, actionable ValueError,
    # never an unhandled ImportError traceback. Forced via sys.modules rather than
    # relying on the ambient test environment actually lacking the package: CI installs
    # every extra (`uv sync --all-extras --dev`), so "not installed" must be simulated
    # to be tested at all, deterministically, on every machine this runs on.
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(ValueError, match="keyring") as excinfo:
        resolve_secret_reference("keyring:myservice/myaccount")
    assert "myservice/myaccount" in str(excinfo.value)
    assert "extra" in str(excinfo.value)


@pytest.mark.unit
def test_keyring_indirection_requires_service_slash_account() -> None:
    with pytest.raises(ValueError, match="SERVICE/ACCOUNT"):
        resolve_secret_reference("keyring:no-slash-here")


class _FakeKeyring:
    """A minimal stand-in for the real ``keyring`` package's module-level API — just
    ``get_password``, the only function ``resolve_secret_reference`` calls. Lets the
    two tests below exercise "the extra *is* installed" deterministically, without
    depending on (or mutating) any real OS keychain."""

    def __init__(self, value: str | None) -> None:
        self._value = value
        self.calls: list[tuple[str, str]] = []

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append((service, account))
        return self._value


@pytest.mark.unit
def test_keyring_indirection_with_the_extra_installed_resolves_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKeyring("s3cr3t-from-keychain")
    monkeypatch.setitem(sys.modules, "keyring", fake)
    resolved = resolve_secret_reference("keyring:myservice/myaccount")
    assert resolved == "s3cr3t-from-keychain"
    assert fake.calls == [("myservice", "myaccount")]


@pytest.mark.unit
def test_keyring_indirection_with_the_extra_installed_but_no_value_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring(None))
    with pytest.raises(ValueError, match="returned no value") as excinfo:
        resolve_secret_reference("keyring:myservice/myaccount")
    assert "myservice/myaccount" in str(excinfo.value)


@pytest.mark.unit
def test_env_indirected_api_key_is_resolved_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("N8N_KEY_FOR_TEST", "the-real-key")
    settings = _settings(n8n_api_key="env:N8N_KEY_FOR_TEST")
    assert settings.n8n_api_key.get_secret_value() == "the-real-key"


# --------------------------------------------------------------------------------------
# Secret redaction — repr, str, and error paths never carry a raw secret value.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_repr_never_contains_the_raw_api_key() -> None:
    settings = _settings(n8n_api_key="extremely-sensitive-value-9f8e")
    assert "extremely-sensitive-value-9f8e" not in repr(settings)
    assert "extremely-sensitive-value-9f8e" not in str(settings)


@pytest.mark.unit
def test_repr_never_contains_the_raw_bearer_token() -> None:
    settings = _settings(
        http_bind="0.0.0.0:9000",
        http_bearer_token="bearer-token-should-not-leak",
        http_allowed_origins="https://client.example.com",
    )
    assert "bearer-token-should-not-leak" not in repr(settings)
    assert "bearer-token-should-not-leak" not in str(settings)


@pytest.mark.unit
def test_secret_fields_are_secretstr_instances() -> None:
    settings = _settings(http_bearer_token="x")
    assert isinstance(settings.n8n_api_key, SecretStr)
    assert isinstance(settings.http_bearer_token, SecretStr)


@pytest.mark.unit
def test_load_settings_error_never_leaks_a_successfully_resolved_secret() -> None:
    """The scenario that actually matters: a real secret resolves successfully, a
    *different* field then fails validation, and the raised ConfigurationError must not
    carry the resolved secret anywhere — not in ``message``, not in ``details``."""
    real_secret = "sk-live-do-not-leak-this-abc123"
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(
            _env_file=None,
            n8n_base_url=REQUIRED["n8n_base_url"],
            n8n_api_key=real_secret,
            approval_bind="0.0.0.0:8765",  # non-loopback -> boundary B10 failure
        )
    error = excinfo.value
    assert real_secret not in error.message
    assert real_secret not in repr(error.details)
    assert real_secret not in repr(error.to_dict())
    # And the error is still informative about what actually went wrong:
    assert "approval_bind" in error.message


@pytest.mark.unit
def test_load_settings_builds_its_message_from_loc_and_msg_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exc.errors()`` entries carry an ``input`` key that can echo back an invalid raw
    value; ``load_settings`` must build its message only from each error's ``loc`` and
    ``msg``. An unresolvable ``env:`` reference is a case where ``input`` (the reference
    string) and ``msg`` (our own ValueError text, which legitimately names that same
    reference — reference *names* are not secrets) happen to overlap, so this asserts the
    stronger, structural property directly against the raw pydantic error rather than
    trying to tell the two apart by string content."""
    monkeypatch.delenv("MISSING_FOR_TEST", raising=False)
    from pydantic import ValidationError

    bad_kwargs: dict[str, object] = {
        "n8n_base_url": REQUIRED["n8n_base_url"],
        "n8n_api_key": "env:MISSING_FOR_TEST",
    }
    with pytest.raises(ValidationError) as raw_excinfo:
        Settings(_env_file=None, **bad_kwargs)  # type: ignore[call-arg, arg-type]
    raw_error = raw_excinfo.value.errors()[0]
    assert raw_error["input"] == "env:MISSING_FOR_TEST"

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(
            _env_file=None,
            n8n_base_url=REQUIRED["n8n_base_url"],
            n8n_api_key="env:MISSING_FOR_TEST",
        )
    # The message is exactly `loc: msg`, not a dump of the raw pydantic error object.
    assert excinfo.value.message == f"invalid configuration: n8n_api_key: {raw_error['msg']}"


# --------------------------------------------------------------------------------------
# Startup validation — approval_bind, http_bind, database_url, numeric bounds
# --------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("bind", ["0.0.0.0:8765", "192.168.1.1:8765", "example.com:8765"])
def test_approval_bind_must_be_loopback(bind: str) -> None:
    with pytest.raises(ConfigurationError, match="loopback"):
        load_settings(_env_file=None, **REQUIRED, approval_bind=bind)


@pytest.mark.unit
@pytest.mark.parametrize("bind", ["127.0.0.1:8765", "localhost:9999", "[::1]:8765"])
def test_approval_bind_accepts_loopback_forms(bind: str) -> None:
    settings = _settings(approval_bind=bind)
    assert settings.approval_bind == bind


@pytest.mark.unit
def test_http_bind_non_loopback_requires_bearer_token_and_origins() -> None:
    with pytest.raises(ConfigurationError, match="boundary B9"):
        load_settings(_env_file=None, **REQUIRED, http_bind="0.0.0.0:8000")


@pytest.mark.unit
def test_http_bind_non_loopback_requires_both_not_just_one() -> None:
    with pytest.raises(ConfigurationError, match="boundary B9"):
        load_settings(
            _env_file=None,
            **REQUIRED,
            http_bind="0.0.0.0:8000",
            http_bearer_token="a-token",
            # no http_allowed_origins
        )
    with pytest.raises(ConfigurationError, match="boundary B9"):
        load_settings(
            _env_file=None,
            **REQUIRED,
            http_bind="0.0.0.0:8000",
            http_allowed_origins="https://client.example.com",
            # no http_bearer_token
        )


@pytest.mark.unit
def test_http_bind_non_loopback_with_both_guards_succeeds() -> None:
    settings = _settings(
        http_bind="0.0.0.0:8000",
        http_bearer_token="a-token",
        http_allowed_origins="https://client.example.com, https://other.example.com",
    )
    assert settings.allowed_origins() == (
        "https://client.example.com",
        "https://other.example.com",
    )


@pytest.mark.unit
def test_http_bind_loopback_needs_no_guard() -> None:
    settings = _settings(http_bind="127.0.0.1:8000")
    assert settings.http_bearer_token is None


@pytest.mark.unit
@pytest.mark.parametrize("bad_url", ["not a url at all", "mysql://user@host/db"])
def test_database_url_rejects_unsupported_or_malformed(bad_url: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(_env_file=None, **REQUIRED, database_url=bad_url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "good_url",
    ["sqlite+pysqlite:///./x.db", "sqlite:///./x.db", "postgresql+psycopg://u:p@h/db"],
)
def test_database_url_accepts_supported_drivers(good_url: str) -> None:
    settings = _settings(database_url=good_url)
    assert settings.database_url == good_url


@pytest.mark.unit
@pytest.mark.parametrize("field", ["request_timeout_seconds", "max_argument_bytes"])
def test_non_positive_numeric_settings_are_rejected(field: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(_env_file=None, **REQUIRED, **{field: 0})
    with pytest.raises(ConfigurationError):
        load_settings(_env_file=None, **REQUIRED, **{field: -1})


@pytest.mark.unit
def test_approval_url_exposure_rejects_unknown_values() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(_env_file=None, **REQUIRED, approval_url_exposure="sometimes")


@pytest.mark.unit
@pytest.mark.parametrize("level", ["debug", "Info", "WARNING", "error", "CRITICAL"])
def test_log_level_is_case_insensitive(level: str) -> None:
    settings = _settings(log_level=level)
    assert settings.log_level == level.upper()


@pytest.mark.unit
def test_log_level_rejects_unknown_values() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(_env_file=None, **REQUIRED, log_level="VERBOSE")


@pytest.mark.unit
@pytest.mark.parametrize("bad_bind", ["no-port-here", "host:not-a-number", ":8765", "host:"])
def test_bind_addresses_must_be_host_colon_port(bad_bind: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(_env_file=None, **REQUIRED, approval_bind=bad_bind)


# --------------------------------------------------------------------------------------
# resolve_database_url — used by Alembic and the db CLI, independent of full Settings
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_database_url_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("N8N_OPERATOR_DATABASE_URL", raising=False)
    assert resolve_database_url() == DEFAULT_DATABASE_URL


@pytest.mark.unit
def test_resolve_database_url_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", "sqlite+pysqlite:///./from-env.db")
    assert resolve_database_url() == "sqlite+pysqlite:///./from-env.db"


@pytest.mark.unit
def test_resolve_database_url_explicit_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", "sqlite+pysqlite:///./from-env.db")
    assert (
        resolve_database_url("sqlite+pysqlite:///./explicit.db")
        == "sqlite+pysqlite:///./explicit.db"
    )


@pytest.mark.unit
def test_resolve_database_url_validates_like_settings_does() -> None:
    with pytest.raises(ValueError, match="not supported"):
        resolve_database_url("mysql://user@host/db")


@pytest.mark.unit
def test_resolve_database_url_requires_no_settings_fields_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of this function (ADR-006-adjacent design note in config.py):
    it must not require N8N_BASE_URL/N8N_API_KEY to be set."""
    for var in ("N8N_OPERATOR_N8N_BASE_URL", "N8N_OPERATOR_N8N_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_database_url() == DEFAULT_DATABASE_URL
