# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Set up

```bash
git clone https://github.com/manoj-maharana/kiro-a2a-bridge
cd kiro-a2a-bridge
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

The tests need no accounts or network. They use the built-in demo ACP agent
(`src/kiro_a2a/demo_agent.py`) and a fake OpenID Connect provider (`tests/fake_idp.py`).

Try things by hand without Kiro:

```bash
kiro-a2a serve --agent demo
kiro-a2a ask http://127.0.0.1:8765 "hello" --file README.md
```

## Guidelines

- Keep the core dependency list at `aiohttp` only. Optional features go behind extras.
- Every change that alters behaviour gets a test.
- Keep names and examples generic: no company, product or person names in code, docs or tests.
- Security-relevant defaults stay closed: push notifications off, `read-only` permissions,
  bind to `127.0.0.1`.
- Update `CHANGELOG.md`.

## Releasing (maintainers)

Before the first release, put your GitHub username into the README image links and project URLs:

```bash
grep -rl manoj-maharana README.md pyproject.toml CONTRIBUTING.md | xargs sed -i 's/manoj-maharana/<github-user>/g'
```

PyPI shows README images only from absolute URLs, so the images appear once the repository is public.

1. Bump `__version__` in `src/kiro_a2a/__init__.py` and move the changelog entry out of "unreleased".
2. Commit, then tag: `git tag v0.2.0 && git push --tags`.
3. The `Release` workflow publishes to PyPI (trusted publishing), creates the GitHub release,
   and pushes the Docker image to GHCR.
