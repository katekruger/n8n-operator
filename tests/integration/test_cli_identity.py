"""``n8n-operator identity`` end to end, through the real Typer CLI (stage 02)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from n8n_operator import logging_setup
from n8n_operator.cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_OPERATOR_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.delenv("N8N_OPERATOR_N8N_BASE_URL", raising=False)
    monkeypatch.delenv("N8N_OPERATOR_N8N_API_KEY", raising=False)


def _init(cli_env: None) -> None:
    result = runner.invoke(app, ["db", "init"])
    assert result.exit_code == 0, result.output


_REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: identity-cli-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync contact
    description: Read-only sync.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash="a" * 64)


def _load_registry(cli_env: None, tmp_path: Path) -> None:
    registry_path = tmp_path / "workflows.yaml"
    registry_path.write_text(_REGISTRY_YAML)
    result = runner.invoke(app, ["registry", "reload", "--path", str(registry_path)])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
def test_bootstrap_creates_an_organization_and_its_first_admin(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(
        app,
        [
            "identity",
            "bootstrap",
            "--org-name",
            "Acme",
            "--admin-issuer",
            "https://idp.example.com",
            "--admin-subject",
            "kate",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Organization created" in result.output
    assert "Admin principal" in result.output

    list_result = runner.invoke(app, ["identity", "list-orgs"])
    assert "Acme" in list_result.output


@pytest.mark.integration
def test_add_membership_grants_a_new_principal_a_role(cli_env: None) -> None:
    _init(cli_env)
    runner.invoke(
        app,
        [
            "identity",
            "bootstrap",
            "--org-name",
            "Acme",
            "--admin-issuer",
            "https://idp.example.com",
            "--admin-subject",
            "kate",
        ],
    )
    org_id = runner.invoke(app, ["identity", "list-orgs"]).output.split()[0]

    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            "viewer,operator",
            "--display-name",
            "Bob",
        ],
    )
    assert result.exit_code == 0, result.output

    memberships = runner.invoke(app, ["identity", "list-memberships", "--org", org_id])
    assert "Bob" in memberships.output
    assert "viewer" in memberships.output
    assert "operator" in memberships.output


@pytest.mark.integration
def test_add_membership_rejects_an_invalid_role(cli_env: None) -> None:
    _init(cli_env)
    runner.invoke(
        app,
        [
            "identity",
            "bootstrap",
            "--org-name",
            "Acme",
            "--admin-issuer",
            "https://idp.example.com",
            "--admin-subject",
            "kate",
        ],
    )
    org_id = runner.invoke(app, ["identity", "list-orgs"]).output.split()[0]

    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            "superuser",
        ],
    )
    assert result.exit_code == 1
    assert "Invalid role" in result.output


@pytest.mark.integration
def test_remove_membership_then_list_memberships_shows_it_removed(cli_env: None) -> None:
    _init(cli_env)
    runner.invoke(
        app,
        [
            "identity",
            "bootstrap",
            "--org-name",
            "Acme",
            "--admin-issuer",
            "https://idp.example.com",
            "--admin-subject",
            "kate",
        ],
    )
    org_id = runner.invoke(app, ["identity", "list-orgs"]).output.split()[0]
    runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            "viewer",
            "--display-name",
            "Bob",
        ],
    )
    memberships_before = runner.invoke(app, ["identity", "list-memberships", "--org", org_id])
    principal_id = next(
        line.split()[0] for line in memberships_before.output.splitlines() if "Bob" in line
    )

    remove_result = runner.invoke(
        app, ["identity", "remove-membership", "--org", org_id, "--principal", principal_id]
    )
    assert remove_result.exit_code == 0, remove_result.output

    memberships_after = runner.invoke(app, ["identity", "list-memberships", "--org", org_id])
    assert "Bob" not in memberships_after.output
    assert "kate" in memberships_after.output  # the bootstrap admin's membership persists

    memberships_with_removed = runner.invoke(
        app, ["identity", "list-memberships", "--org", org_id, "--include-removed"]
    )
    assert "removed" in memberships_with_removed.output


@pytest.mark.integration
def test_create_service_principal_requires_a_resolvable_credential_ref(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(
        app,
        [
            "identity",
            "create-service-principal",
            "--name",
            "CI bot",
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_UNSET_TOKEN_XYZ",
        ],
    )
    assert result.exit_code == 1
    assert "does not resolve" in result.output


@pytest.mark.integration
def test_create_service_principal_never_prints_the_resolved_secret(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(cli_env)
    monkeypatch.setenv("N8N_OPERATOR_TEST_CI_TOKEN", "super-secret-value-000000")
    result = runner.invoke(
        app,
        [
            "identity",
            "create-service-principal",
            "--name",
            "CI bot",
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_CI_TOKEN",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "super-secret-value-000000" not in result.output
    assert "env:N8N_OPERATOR_TEST_CI_TOKEN" in result.output


@pytest.mark.integration
def test_create_service_principal_registers_the_resolved_secret_for_log_scrubbing(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage 02 negative case "log/result secret leaks": validating a
    ``credential_ref`` resolves it, however transiently — the resolved value must be
    registered with ``logging_setup.register_secret`` so it is scrubbed from this same
    invocation's own structured logs, mirroring ``serve.py``'s ``n8n_api_key``/
    ``http_bearer_token`` registration at startup."""
    logging_setup._reset_registered_secrets_for_tests()
    _init(cli_env)
    monkeypatch.setenv("N8N_OPERATOR_TEST_CI_TOKEN", "cli-scrub-worthy-value-000000")
    try:
        result = runner.invoke(
            app,
            [
                "identity",
                "create-service-principal",
                "--name",
                "CI bot",
                "--credential-ref",
                "env:N8N_OPERATOR_TEST_CI_TOKEN",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "cli-scrub-worthy-value-000000" in logging_setup._known_secrets
    finally:
        logging_setup._reset_registered_secrets_for_tests()


@pytest.mark.integration
def test_disable_and_enable_a_service_principal(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(cli_env)
    monkeypatch.setenv("N8N_OPERATOR_TEST_CI_TOKEN", "a-token-000000")
    create_result = runner.invoke(
        app,
        [
            "identity",
            "create-service-principal",
            "--name",
            "CI bot",
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_CI_TOKEN",
        ],
    )
    principal_id = create_result.output.splitlines()[0].split(": ")[1].split(" (")[0]

    disable_result = runner.invoke(app, ["identity", "disable-principal", principal_id])
    assert disable_result.exit_code == 0, disable_result.output
    listing = runner.invoke(app, ["identity", "list-service-principals"])
    assert "disabled" in listing.output

    enable_result = runner.invoke(app, ["identity", "enable-principal", principal_id])
    assert enable_result.exit_code == 0, enable_result.output
    listing_after = runner.invoke(app, ["identity", "list-service-principals"])
    assert "enabled" in listing_after.output
    assert "disabled" not in listing_after.output


@pytest.mark.integration
def test_disable_principal_fails_cleanly_for_an_unknown_id(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["identity", "disable-principal", "nonexistent"])
    assert result.exit_code == 1
    assert "no such principal" in result.output.lower()


@pytest.mark.integration
def test_rotate_service_credential_never_prints_the_new_secret(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(cli_env)
    monkeypatch.setenv("N8N_OPERATOR_TEST_CI_TOKEN", "old-token-000000")
    monkeypatch.setenv("N8N_OPERATOR_TEST_CI_TOKEN_NEW", "brand-new-secret-000000")
    create_result = runner.invoke(
        app,
        [
            "identity",
            "create-service-principal",
            "--name",
            "CI bot",
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_CI_TOKEN",
        ],
    )
    principal_id = create_result.output.splitlines()[0].split(": ")[1].split(" (")[0]

    rotate_result = runner.invoke(
        app,
        [
            "identity",
            "rotate-service-credential",
            principal_id,
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_CI_TOKEN_NEW",
        ],
    )
    assert rotate_result.exit_code == 0, rotate_result.output
    assert "brand-new-secret-000000" not in rotate_result.output
    assert "env:N8N_OPERATOR_TEST_CI_TOKEN_NEW" in rotate_result.output


@pytest.mark.integration
def test_add_membership_to_a_nonexistent_organization_fails_cleanly(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            "nonexistent",
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            "viewer",
        ],
    )
    assert result.exit_code == 1
    assert "No such organization" in result.output


@pytest.mark.integration
def test_list_orgs_when_empty(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["identity", "list-orgs"])
    assert result.exit_code == 0
    assert "No organizations" in result.output


@pytest.mark.integration
def test_list_service_principals_when_empty(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["identity", "list-service-principals"])
    assert result.exit_code == 0
    assert "No service principals" in result.output


@pytest.mark.integration
def test_list_memberships_when_empty(cli_env: None) -> None:
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Empty Co"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(app, ["identity", "list-memberships", "--org", org_id])
    assert result.exit_code == 0
    assert "No memberships" in result.output


@pytest.mark.integration
def test_list_memberships_for_a_nonexistent_organization(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["identity", "list-memberships", "--org", "nonexistent"])
    assert result.exit_code == 1
    assert "No such organization" in result.output


@pytest.mark.integration
def test_add_membership_refuses_an_already_disabled_principal(cli_env: None) -> None:
    _init(cli_env)
    runner.invoke(
        app,
        [
            "identity",
            "bootstrap",
            "--org-name",
            "Acme",
            "--admin-issuer",
            "https://idp.example.com",
            "--admin-subject",
            "kate",
        ],
    )
    org_id = runner.invoke(app, ["identity", "list-orgs"]).output.split()[0]
    runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            "viewer",
        ],
    )
    memberships = runner.invoke(app, ["identity", "list-memberships", "--org", org_id])
    principal_id = next(
        line.split()[0] for line in memberships.output.splitlines() if "bob" in line
    )
    runner.invoke(app, ["identity", "disable-principal", principal_id])

    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            "operator",
        ],
    )
    assert result.exit_code == 1
    assert "disabled" in result.output.lower()


@pytest.mark.integration
def test_add_membership_refuses_an_empty_roles_list(cli_env: None) -> None:
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "bob",
            "--roles",
            " , ",
        ],
    )
    assert result.exit_code == 1
    assert "At least one role is required" in result.output


@pytest.mark.integration
def test_add_membership_refuses_a_duplicate_active_membership(cli_env: None) -> None:
    _init(cli_env)
    runner.invoke(
        app,
        [
            "identity",
            "bootstrap",
            "--org-name",
            "Acme",
            "--admin-issuer",
            "https://idp.example.com",
            "--admin-subject",
            "kate",
        ],
    )
    org_id = runner.invoke(app, ["identity", "list-orgs"]).output.split()[0]

    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "kate",
            "--roles",
            "viewer",
        ],
    )
    assert result.exit_code == 1
    assert "already has an active membership" in result.output


@pytest.mark.integration
def test_remove_membership_for_a_pair_with_no_active_membership(cli_env: None) -> None:
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app, ["identity", "remove-membership", "--org", org_id, "--principal", "nonexistent"]
    )
    assert result.exit_code == 1
    assert "No active membership" in result.output


@pytest.mark.integration
def test_enable_principal_fails_cleanly_for_an_unknown_id(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["identity", "enable-principal", "nonexistent"])
    assert result.exit_code == 1
    assert "no such principal" in result.output.lower()


@pytest.mark.integration
def test_rotate_service_credential_fails_cleanly_for_an_unknown_principal(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(cli_env)
    monkeypatch.setenv("N8N_OPERATOR_TEST_TOKEN", "a-token-000000")
    result = runner.invoke(
        app,
        [
            "identity",
            "rotate-service-credential",
            "nonexistent",
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_TOKEN",
        ],
    )
    assert result.exit_code == 1
    assert "no such principal" in result.output.lower()


@pytest.mark.integration
def test_list_service_principals_exclude_disabled_flag(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(cli_env)
    monkeypatch.setenv("N8N_OPERATOR_TEST_TOKEN", "a-token-000000")
    create_result = runner.invoke(
        app,
        [
            "identity",
            "create-service-principal",
            "--name",
            "CI bot",
            "--credential-ref",
            "env:N8N_OPERATOR_TEST_TOKEN",
        ],
    )
    principal_id = create_result.output.splitlines()[0].split(": ")[1].split(" (")[0]
    runner.invoke(app, ["identity", "disable-principal", principal_id])

    with_disabled = runner.invoke(app, ["identity", "list-service-principals"])
    assert "CI bot" in with_disabled.output

    without_disabled = runner.invoke(
        app, ["identity", "list-service-principals", "--exclude-disabled"]
    )
    assert "CI bot" not in without_disabled.output
    assert "No service principals" in without_disabled.output


@pytest.mark.integration
def test_identity_help_lists_every_subcommand() -> None:
    result = runner.invoke(app, ["identity", "--help"])
    assert result.exit_code == 0
    for name in (
        "bootstrap",
        "create-org",
        "list-orgs",
        "add-membership",
        "remove-membership",
        "list-memberships",
        "disable-principal",
        "enable-principal",
        "create-service-principal",
        "rotate-service-credential",
        "list-service-principals",
        "preview-permissions",
    ):
        assert name in result.stdout


@pytest.mark.integration
def test_add_membership_rejects_a_workflow_scope_matching_nothing(
    cli_env: None, tmp_path: Path
) -> None:
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "operator",
            "--workflow-scope",
            "billing.*",
        ],
    )
    assert result.exit_code == 1
    assert "matches no workflow" in result.output


@pytest.mark.integration
def test_add_membership_accepts_a_workflow_scope_matching_a_real_workflow(
    cli_env: None, tmp_path: Path
) -> None:
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "operator",
            "--workflow-scope",
            "crm.*",
        ],
    )
    assert result.exit_code == 0, result.output


@pytest.mark.integration
def test_add_membership_rejects_an_environment_scope_naming_a_nonexistent_environment(
    cli_env: None,
) -> None:
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "viewer",
            "--environment-scope",
            "env-does-not-exist",
        ],
    )
    assert result.exit_code == 1
    assert "do not exist in this organization" in result.output


@pytest.mark.integration
def test_add_membership_rejects_a_garbled_case_role_rather_than_silently_storing_it(
    cli_env: None,
) -> None:
    """Case normalization: a role string that doesn't exactly match
    ``viewer``/``operator``/``approver``/``admin`` is rejected loudly at grant time —
    never silently stored as an unmatched string that would later authorize nothing
    while looking, to a casual reader of ``list-memberships``, like a real grant."""
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "Admin",
        ],
    )
    assert result.exit_code == 1
    assert "Invalid role" in result.output


@pytest.mark.integration
def test_add_membership_of_admin_prompts_for_confirmation_and_aborts_on_no(
    cli_env: None,
) -> None:
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "admin",
        ],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "Not granted" in result.output


@pytest.mark.integration
def test_add_membership_of_admin_with_yes_skips_confirmation(cli_env: None) -> None:
    _init(cli_env)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    result = runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "admin",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output


@pytest.mark.integration
def test_preview_permissions_for_a_principal_with_no_memberships(cli_env: None) -> None:
    _init(cli_env)
    result = runner.invoke(app, ["identity", "preview-permissions", "no-such-principal"])
    assert result.exit_code == 0
    assert "authorized for nothing" in result.output


@pytest.mark.integration
def test_preview_permissions_reflects_a_real_grant(cli_env: None, tmp_path: Path) -> None:
    _init(cli_env)
    _load_registry(cli_env, tmp_path)
    org_result = runner.invoke(app, ["identity", "create-org", "--name", "Acme"])
    org_id = org_result.output.split(": ")[1].split(" (")[0]
    runner.invoke(
        app,
        [
            "identity",
            "add-membership",
            "--org",
            org_id,
            "--issuer",
            "https://idp.example.com",
            "--subject",
            "alice",
            "--roles",
            "viewer",
        ],
    )
    memberships = runner.invoke(app, ["identity", "list-memberships", "--org", org_id])
    principal_id = next(
        line.split()[0] for line in memberships.output.splitlines() if "alice" in line
    )

    result = runner.invoke(app, ["identity", "preview-permissions", principal_id])
    assert result.exit_code == 0
    assert "list_workflows" in result.output
    assert "prepare_operation" not in result.output.split("Denied")[0]  # viewer: not allowed
    assert "prepare_operation" in result.output.split("Denied")[1]
