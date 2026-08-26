"""Entry point for ``python -m n8n_operator``.

Delegates to the Typer application in :mod:`n8n_operator.cli.main`.
Not implemented in phase 0 (see BUILD_PLAN section 12, phase 1).
"""

from __future__ import annotations


def main() -> None:
    """Run the CLI. Implemented in phase 1."""
    raise NotImplementedError(
        "n8n Operator is in the architecture/bootstrap phase; the CLI lands in phase 1."
    )


if __name__ == "__main__":
    main()
