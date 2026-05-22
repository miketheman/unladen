# Contributing to unladen

Thanks for your interest in `unladen`.
These notes apply to everyone working on the project:
human contributors and AI coding agents alike.

> [!IMPORTANT]
> `unladen` is in early alpha;
> its scope and direction are still settling.
> Please open an issue before writing code.

## Open an issue first

Before starting work,
and well before opening a pull request,
[open an issue](https://github.com/miketheman/unladen/issues)
describing the bug or change you have in mind.

This is not a formality.
Discussing first lets us agree on the approach,
surface duplicate or conflicting work early,
and decide whether a change fits the project's direction
before anyone spends time building it.
A short conversation up front
saves a rejected pull request later.

Ideas already considered and deferred
live in [FUTURE.md](../FUTURE.md).
Check it before proposing something new.

A pull request that arrives without a prior issue
may be asked to start with one.

## Development setup

You will need:

- Python 3.14 or newer
- [uv](https://docs.astral.sh/uv/) for packaging and environments
- [just](https://github.com/casey/just) as the command runner

Install the project and its development dependencies:

```bash
just install
```

## Workflow

The [Justfile](../Justfile) is the source of truth for project commands.
Run `just` (or `just --list`) to see them all;
these are the ones you will use most:

| Recipe | What it does |
| ------ | ------------ |
| `just test` | Run the test suite |
| `just cov` | Run tests with a coverage report |
| `just lint` | `ruff` lint and format check |
| `just fmt` | Auto-fix lint issues and format the code |
| `just typecheck` | Type-check with `ty` |
| `just check` | Lint, type-check, and test |
| `just run <args>` | Run the `unladen` CLI locally |

`unladen` is developed test-first:
add a failing test that captures the bug or feature,
then make it pass.
New behavior needs tests.

Before opening a pull request, run `just check`.
Every test must pass,
and coverage must stay at or above 95%
(enforced in `pyproject.toml`).

## Code style

`ruff` handles formatting and linting.
Run `just fmt` before committing.
Prose in docs and docstrings uses
[semantic line breaks](https://sembr.org).
Keep changes minimal and focused;
avoid unrelated refactors in the same pull request.
No emojis in code or CLI output.

## Releasing

For maintainers.
`just release-version` drops the `.devN` marker for a release;
pushing a `v*` tag then triggers the release workflow.
`just bump-dev` opens the next development cycle.
