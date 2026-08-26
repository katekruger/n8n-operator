"""Entry point for ``python -m n8n_operator``.

Delegates to the same Typer application the installed ``n8n-operator`` script uses
(``n8n_operator.cli.main:app``), so the two invocation styles behave identically.
"""

from __future__ import annotations

from n8n_operator.cli.main import app

__all__ = ["app", "main"]


def main() -> None:
    app()


if __name__ == "__main__":
    main()
