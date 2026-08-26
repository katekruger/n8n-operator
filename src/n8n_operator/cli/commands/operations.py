"""``n8n-operator operations`` — list, show, approve, reject, cancel, expire.

``approve`` and ``reject`` are the **canonical** v1 approval channel; the loopback
approval page is a convenience alternative over the same core use case (ADR-010).

``expire`` applies all overdue T08/T11 transitions on demand, for deployments that run no
approval app. It is a maintenance convenience only: lazy transactional expiry is
authoritative, so no expired operation is ever executable regardless of whether this has
run (invariant I9).

Phases 6 and 8 (BUILD_PLAN section 12).
"""

from __future__ import annotations
