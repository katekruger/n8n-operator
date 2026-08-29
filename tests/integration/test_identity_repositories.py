"""The v2 identity repositories (``OrganizationRepository``,
``OrganizationMembershipRepository``, ``EnvironmentRepository``, and
``PrincipalRepository``'s stage-02 additions) and ``core.identity.ensure_dev_principal``
— against a real SQLite database (``tests/conftest.py``'s ``session_factory``).
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core.identity import DEV_PRINCIPAL_DISPLAY_NAME, ensure_dev_principal
from n8n_operator.storage.models import Environment, utc_now
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope


class TestOrganizationRepository:
    def test_create_and_get(self, session_factory: sessionmaker[Session]) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            org_id = org.id
        with session_factory() as session:
            found = OrganizationRepository(session).get(org_id)
            assert found is not None
            assert found.name == "Acme"

    def test_get_missing_returns_none(self, session_factory: sessionmaker[Session]) -> None:
        with session_factory() as session:
            assert OrganizationRepository(session).get("nonexistent") is None

    def test_list_returns_every_organization_in_creation_order(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            OrganizationRepository(session).create(name="First")
            OrganizationRepository(session).create(name="Second")
        with session_factory() as session:
            names = [org.name for org in OrganizationRepository(session).list()]
        assert names == ["First", "Second"]


class TestOrganizationMembershipRepository:
    def test_create_requires_at_least_one_role(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            try:
                OrganizationMembershipRepository(session).create(
                    principal_id=principal.id, organization_id=org.id, roles=[]
                )
                raise AssertionError("expected ValueError for an empty roles list")
            except ValueError:
                pass

    def test_environment_scope_defaults_to_wildcard(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            membership = OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=org.id, roles=["viewer"]
            )
        assert membership.environment_scope == ["*"]
        assert membership.workflow_scope == "*"

    def test_get_active_returns_none_after_removal(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            membership = OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=org.id, roles=["viewer"]
            )
            org_id, principal_id, membership_id = org.id, principal.id, membership.id

        with session_factory() as session:
            found = OrganizationMembershipRepository(session).get_active(
                principal_id=principal_id, organization_id=org_id
            )
            assert found is not None

        with session_scope(session_factory) as session:
            OrganizationMembershipRepository(session).remove(membership_id)

        with session_factory() as session:
            found_after = OrganizationMembershipRepository(session).get_active(
                principal_id=principal_id, organization_id=org_id
            )
            assert found_after is None

    def test_a_principal_can_hold_active_memberships_in_two_organizations(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """ADR-013 section 2: one OIDC subject may belong to multiple organizations —
        the repository layer must not artificially restrict that."""
        with session_scope(session_factory) as session:
            org_a = OrganizationRepository(session).create(name="A")
            org_b = OrganizationRepository(session).create(name="B")
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=org_a.id, roles=["admin"]
            )
            OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=org_b.id, roles=["viewer"]
            )
            principal_id = principal.id

        with session_factory() as session:
            active = OrganizationMembershipRepository(session).list_active_for_principal(
                principal_id
            )
        assert {m.organization_id for m in active} == {org_a.id, org_b.id}

    def test_removing_one_membership_does_not_remove_the_other(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """The ``active_organization_id`` NULL-uniqueness trick (storage/models.py)
        must key on ``(principal_id, organization_id)`` via the sentinel column, never
        collide across two genuinely different organizations for the same principal."""
        with session_scope(session_factory) as session:
            org_a = OrganizationRepository(session).create(name="A")
            org_b = OrganizationRepository(session).create(name="B")
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            membership_a = OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=org_a.id, roles=["admin"]
            )
            OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=org_b.id, roles=["viewer"]
            )
            principal_id = principal.id

        with session_scope(session_factory) as session:
            OrganizationMembershipRepository(session).remove(membership_a.id)

        with session_factory() as session:
            active = OrganizationMembershipRepository(session).list_active_for_principal(
                principal_id
            )
        assert {m.organization_id for m in active} == {org_b.id}

    def test_list_for_organization_excludes_removed_by_default(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            p1 = PrincipalRepository(session).create(kind="service", display_name="one")
            p2 = PrincipalRepository(session).create(kind="service", display_name="two")
            OrganizationMembershipRepository(session).create(
                principal_id=p1.id, organization_id=org.id, roles=["viewer"]
            )
            m2 = OrganizationMembershipRepository(session).create(
                principal_id=p2.id, organization_id=org.id, roles=["viewer"]
            )
            org_id = org.id

        with session_scope(session_factory) as session:
            OrganizationMembershipRepository(session).remove(m2.id)

        with session_factory() as session:
            active_only = OrganizationMembershipRepository(session).list_for_organization(org_id)
            everything = OrganizationMembershipRepository(session).list_for_organization(
                org_id, include_removed=True
            )
        assert len(active_only) == 1
        assert len(everything) == 2


class TestEnvironmentRepository:
    def test_list_for_organization_excludes_archived_by_default(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            active_env = Environment(
                organization_id=org.id,
                name="staging",
                n8n_base_url_ref="env:STAGING_URL",
                n8n_api_key_ref="env:STAGING_KEY",
            )
            archived_env = Environment(
                organization_id=org.id,
                name="old",
                n8n_base_url_ref="env:OLD_URL",
                n8n_api_key_ref="env:OLD_KEY",
                archived_at=utc_now(),
            )
            session.add_all([active_env, archived_env])
            org_id = org.id

        with session_factory() as session:
            active_only = EnvironmentRepository(session).list_for_organization(org_id)
            everything = EnvironmentRepository(session).list_for_organization(
                org_id, include_archived=True
            )
        assert [e.name for e in active_only] == ["staging"]
        assert len(everything) == 2

    def test_empty_for_an_organization_with_no_environments(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            org = OrganizationRepository(session).create(name="Acme")
            org_id = org.id
        with session_factory() as session:
            assert EnvironmentRepository(session).list_for_organization(org_id) == []


class TestPrincipalRepositoryServiceAdditions:
    def test_enable_clears_disabled_at(self, session_factory: sessionmaker[Session]) -> None:
        with session_scope(session_factory) as session:
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            PrincipalRepository(session).disable(principal.id)
            principal_id = principal.id

        with session_factory() as session:
            stored = PrincipalRepository(session).get(principal_id)
            assert stored is not None
            assert stored.disabled_at is not None

        with session_scope(session_factory) as session:
            PrincipalRepository(session).enable(principal_id)

        with session_factory() as session:
            stored_after = PrincipalRepository(session).get(principal_id)
            assert stored_after is not None
            assert stored_after.disabled_at is None

    def test_disable_and_enable_raise_for_an_unknown_principal(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_factory() as session:
            try:
                PrincipalRepository(session).disable("nonexistent")
                raise AssertionError("expected LookupError")
            except LookupError:
                pass
            try:
                PrincipalRepository(session).enable("nonexistent")
                raise AssertionError("expected LookupError")
            except LookupError:
                pass

    def test_set_credential_ref_updates_and_can_be_cleared(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            principal = PrincipalRepository(session).create(kind="service", display_name="svc")
            principal_id = principal.id

        with session_scope(session_factory) as session:
            PrincipalRepository(session).set_credential_ref(principal_id, "env:SVC_TOKEN")
        with session_factory() as session:
            stored = PrincipalRepository(session).get(principal_id)
            assert stored is not None
            assert stored.credential_ref == "env:SVC_TOKEN"

        with session_scope(session_factory) as session:
            PrincipalRepository(session).set_credential_ref(principal_id, None)
        with session_factory() as session:
            stored_after = PrincipalRepository(session).get(principal_id)
            assert stored_after is not None
            assert stored_after.credential_ref is None

    def test_list_service_principals_excludes_other_kinds(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            PrincipalRepository(session).create(kind="service", display_name="svc-a")
            PrincipalRepository(session).create(kind="service", display_name="svc-b")
            PrincipalRepository(session).create(
                kind="user", display_name="human", external_subject="s", external_issuer="i"
            )
        with session_factory() as session:
            services = PrincipalRepository(session).list_service_principals()
        assert {p.display_name for p in services} == {"svc-a", "svc-b"}

    def test_list_service_principals_can_exclude_disabled(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            PrincipalRepository(session).create(kind="service", display_name="enabled")
            disabled = PrincipalRepository(session).create(kind="service", display_name="disabled")
            PrincipalRepository(session).disable(disabled.id)

        with session_factory() as session:
            only_enabled = PrincipalRepository(session).list_service_principals(
                include_disabled=False
            )
        assert {p.display_name for p in only_enabled} == {"enabled"}

    def test_two_principals_cannot_share_an_external_identity(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """``uq_principals_external_identity`` (storage/models.py, migration 0004) —
        the database-level half of ADR-014's "identity is the pair" guarantee."""
        from sqlalchemy.exc import IntegrityError

        with session_scope(session_factory) as session:
            PrincipalRepository(session).create(
                kind="user",
                display_name="first",
                external_subject="dup",
                external_issuer="https://idp",
            )

        try:
            with session_scope(session_factory) as session:
                PrincipalRepository(session).create(
                    kind="user",
                    display_name="second",
                    external_subject="dup",
                    external_issuer="https://idp",
                )
            raise AssertionError("expected IntegrityError")
        except IntegrityError:
            pass


class TestEnsureDevPrincipal:
    def test_creates_a_clearly_labeled_service_principal(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        with session_scope(session_factory) as session:
            principal = ensure_dev_principal(session, principal_id="dev")
        assert principal.id == "dev"
        assert principal.kind == "service"
        assert principal.display_name == DEV_PRINCIPAL_DISPLAY_NAME
        assert "never for production" in principal.display_name

    def test_is_idempotent_across_calls(self, session_factory: sessionmaker[Session]) -> None:
        with session_scope(session_factory) as session:
            first = ensure_dev_principal(session, principal_id="dev")
            first_id = first.id
        with session_scope(session_factory) as session:
            second = ensure_dev_principal(session, principal_id="dev")
        assert second.id == first_id

        with session_factory() as session:
            from sqlalchemy import func, select

            from n8n_operator.storage.models import Principal

            count = session.execute(
                select(func.count()).select_from(Principal).where(Principal.id == "dev")
            ).scalar_one()
        assert count == 1
