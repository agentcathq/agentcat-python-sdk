# Contributing to AgentCat 🎉

Thank you for your interest in contributing to AgentCat! We're excited to have you join our community of developers building analytics tools for MCP servers.

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/agentcat-python-sdk.git
   cd agentcat-python-sdk
   ```
3. **Install dependencies** using uv (see [Dev environments](#dev-environments) for the legacy variant):
   ```bash
   uv sync --extra community
   ```
4. **Create a branch** for your feature or fix:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/your-bug-fix
   ```

## Dev Environments

AgentCat v2 supports two generations of the MCP ecosystem: the official `mcp` package 1.x and 2.x, and community `fastmcp` 3.x and 4.x. The two `mcp` majors cannot be installed side by side, so development uses two mutually exclusive uv dependency groups, declared as conflicting in `pyproject.toml`:

- `mcp-modern` — `mcp` 2.x + `fastmcp` 4.x (the default group)
- `mcp-legacy` — `mcp` 1.x + `fastmcp` 3.x

**Modern environment (default):**

```bash
uv sync --extra community
```

**Legacy environment** (turn off the default modern group, turn on legacy):

```bash
uv sync --extra community --no-group mcp-modern --group mcp-legacy
```

Note that a bare `uv run` re-syncs to the default (modern) environment, which would silently undo a legacy sync. Once you have synced the generation you want, run against it with `--no-sync`:

```bash
uv run --no-sync pytest
```

**Both generations are expected to pass.** `tests/conftest.py` reads the installed `mcp` major at collection time and skips the modules that target the other generation, so the same `pytest` invocation selects the right subset in either environment — there is nothing to pass by hand. At the time of writing that is 576 passed / 26 skipped on modern and 732 passed / 10 skipped on legacy. CI runs both legs in the `test-dependency-groups` job of `mcp-compatibility.yml`, so a change that only works in the generation you developed in will be caught there.

## Development Process

### Making Changes

1. **Write your code** following our Python standards
2. **Add tests** for new features (required for feature additions)
3. **Run the test suite** to ensure everything passes, in the generation you
   synced:
   ```bash
   uv run --no-sync pytest
   ```
   `--no-sync`: a bare `uv run` re-syncs to the default (modern) groups and
   silently undoes a legacy sync. Both generations are expected to pass — see
   [Dev Environments](#dev-environments).
4. **Check your code** meets our standards:
   ```bash
   uvx ruff check .    # lint the whole repo
   git diff --name-only main -- '*.py' | xargs -r uvx ruff format --check
   ```
   `uvx`, not `uv run` — see [Code Quality](#code-quality) for why. And
   `--check`, scoped to the files you touched: 46 files in this repo are
   unformatted, so a bare `uvx ruff format .` rewrites all of them and buries
   your change in an unrelated 46-file diff. Reformatting debt you did not
   create is welcome as [its own PR](#the-linttype-debt-and-the-ratchet).

   **`xargs -r`, not `$(…)`.** With no path arguments `ruff format` defaults to
   `.` — so on a branch that has touched no Python (a docs change, or before
   you have made any), the substitution expands to nothing and the command
   becomes exactly the whole-repo rewrite this step exists to avoid. `-r` skips
   the run instead.

### Commit Conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/). Your commit messages should be structured as:

```
<type>: <description>

[optional body]
```

**Types:**

- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `test`: Adding or updating tests
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `chore`: Changes to build process or auxiliary tools

**Examples:**

```bash
git commit -m "feat: add telemetry exporters for observability"
git commit -m "fix: handle edge case in session tracking"
git commit -m "docs: update API documentation"
```

## Pull Request Process

1. **Push your changes** to your fork:

   ```bash
   git push origin feature/your-feature-name
   ```

2. **Create a Pull Request** from your fork to our `main` branch

3. **Fill out the PR description** with:

   - What changes you've made
   - Why these changes are needed
   - Any relevant context or screenshots

4. **Wait for review** - The AgentCat team will review your PR within 2 business days

5. **Address feedback** if any changes are requested

6. **Celebrate** 🎉 once your PR is merged!

### No Issue Required

You don't need to open an issue before submitting a PR. Feel free to submit pull requests directly with your improvements!

## Good First Issues

Looking for a place to start? Check out issues labeled [`good first issue`](https://github.com/agentcathq/agentcat-python-sdk/labels/good%20first%20issue) - these are great for newcomers to the codebase.

## Testing

- New features **should include tests** to ensure reliability
- Run tests locally with `uv run --no-sync pytest`, in the generation you synced
- We use [pytest](https://docs.pytest.org/) for our test suite
- Test files should be placed in the `tests/` directory with `test_*.py` naming convention

## Code Quality

```bash
# Run tests — this IS gated in CI. `--no-sync` keeps the generation you synced.
uv run --no-sync pytest

# Check code style and linting
uvx ruff check .

# Check formatting (the whole repo; 46 files already fail — see the ratchet)
uvx ruff format --check .

# Format only what you changed. `xargs -r`, never `$(…)`: with no path
# arguments ruff formats `.`, so an empty expansion silently rewrites all 46.
git diff --name-only main -- '*.py' | xargs -r uvx ruff format

# Type checking
uvx mypy src/agentcat
```

**`uvx`, not `uv run`, for the last three.** `ruff` and `mypy` are declared only
in the `dev` *extra*, and no default sync installs an extra —
`[tool.uv] default-groups` selects the `dev` dependency *group*, which holds
`freezegun`, `pytest-asyncio` and `pytest-cov`. So `uv run ruff check .` fails
with `ruff: command not found` in a normally-synced checkout. `uvx` fetches the
tool on demand and needs no environment change, which also keeps these commands
safe to run in either dependency generation.

One consequence worth knowing: `uvx mypy` runs *without* the project's
dependencies, so a handful of its findings are import-resolution noise rather
than real type errors. `uvx --with pydantic mypy src/agentcat` removes the
largest chunk of it. The table below reports the plain `uvx mypy` number, so
the commands above reproduce it exactly.

Note that a bare `uv run pytest` syncs the environment to the **default**
groups first, which is the modern generation (`mcp` 2.x + `fastmcp` 4.x). To
run the legacy generation's suite, sync it explicitly — see
[Dev Environments](#dev-environments) — and then use `uv run --no-sync`.

**Only the tests are gated.** Neither workflow in `.github/workflows/` mentions
ruff or mypy at all, so a lint or type finding will not fail your PR today.
That is what makes the rule below a convention rather than a check.

### The lint/type debt, and the ratchet

Neither tool has ever been clean on this repo, and turning either into a
blocking gate would fail every PR on pre-existing findings. Measured on
`feat/explicit-handles-v2` (`uvx ruff check .`, `uvx mypy src/agentcat`, which
reads the `strict = true` in `pyproject.toml`):

| Check | `main` | this branch |
| --- | --- | --- |
| `ruff check .` | 515 | 259 |
| `ruff format --check .` | 57 files | 46 files |
| `mypy src/agentcat` | 80 in 23 files | 53 in 15 files |

Reproduce them with the three `uvx` commands under [Code Quality](#code-quality),
`ruff format --check .` included — that row counts files the formatter *would*
rewrite, which is why it is the `--check` form and not the rewriting one.

The standing rule is a **ratchet, not a gate: a change may not add findings.**
Compare rule-by-rule against `main` rather than eyeballing the totals — a patch
that fixes ten `E501`s and introduces one `B904` has gone backwards even though
the count fell. Cleaning up debt you did not create is welcome as its own PR,
where it can be reviewed as such.

Most of what remains is mechanical: `UP006`/`UP045` (pre-PEP-585/604 typing
spellings), `E501`, `I001` import ordering, and untyped test helpers.

Measure both sides the same way. Roughly 3 of the 53 mypy findings are the
import-resolution noise described above, so a `uvx mypy` number and a
`uvx --with pydantic mypy` number are not comparable to each other — mixing
them invents an improvement, or hides a regression, that is purely an artifact
of the invocation.

## Release dependencies outside this repo

Some changes here are only half-shipped until another repo moves. Check these
before cutting a release:

- **`agentcat-go-api/api/openapi.yaml` is missing the `agentcat:custom`
  `event_type` enum entry.** `publish_custom_event` emits that type, and the
  backend has accepted it since TS 2.0, but the generated `agentcat-api` client
  still validates against the older enum. This SDK works around it by
  overriding the validator (`Event.event_type_validate_enum` in
  `src/agentcat/types.py`) — **any other consumer of that spec stays broken
  until the enum entry lands.** Adding it upstream lets the override be
  deleted.

## Dependencies

While we don't restrict adding new dependencies, they are generally **discouraged** unless absolutely necessary. If you need to add a dependency:

1. Consider if the functionality can be achieved with existing dependencies
2. Check if the dependency is well-maintained and lightweight
3. Ensure it's compatible with our MIT license
4. Add it using uv: `uv add <package-name>`

## Project Structure

```
agentcat-python-sdk/
├── src/           # Source code
│   └── agentcat/  # Main package
│       ├── modules/      # Core modules
│       ├── thirdparty/   # Vendored dependencies
│       ├── types.py      # Type definitions
│       └── utils.py      # Utility functions
├── tests/         # Test files
├── docs/          # Documentation
└── dist/          # Built distributions (generated)
```

## Community

- **Discord**: Join our [Discord server](https://discord.gg/n9qpyhzp2u) for discussions
- **Documentation**: Visit [docs.agentcat.com](https://docs.agentcat.com) for detailed guides
- **Issues**: Browse [open issues](https://github.com/agentcathq/agentcat-python-sdk/issues) for areas needing help

## Versioning

The AgentCat team handles versioning and releases. Your contributions will be included in the next appropriate release based on semantic versioning principles.

## Recognition

All contributors are recognized in our repository. Your contributions help make AgentCat better for everyone building MCP servers!

## Questions?

If you have questions about contributing, feel free to:

- Ask in our [Discord server](https://discord.gg/n9qpyhzp2u)
- Open a [discussion](https://github.com/agentcathq/agentcat-python-sdk/discussions) on GitHub

Thank you for contributing to AgentCat! 🐱