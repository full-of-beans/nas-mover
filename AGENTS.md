# Agent bootstrap

This repository owns NAS mover planning and transactional file transfers, not
NAS maintenance scheduling, parity, storage lifecycle, or enclosure power.
Read `README.md` and the relevant implementation and tests before changes.
Preserve transactional safety, cancellation behavior, and the 100% coverage gate.
Do not operate live storage or deploy without explicit authorization.

## Python formatting and linting

Ruff is the canonical Python formatter and linter. Follow `pyproject.toml`
and the pinned developer-only version in `requirements-dev.txt`.
Install developer tooling with `python -m pip install -r requirements-dev.txt`.
Before submitting Python changes, run `ruff format --check .` and `ruff check .`;
format locally with `ruff format .`. Review any `ruff check --fix .` changes;
do not use unsafe fixes without a behavior review. CI enforces both checks
through the Required checks job. Keep existing tests and coverage checks.
Do not introduce parallel Black, Flake8, or isort tooling without justification.
