# Development

## Local environment

Run `make setup` from the checkout with uv 0.12.13 and a supported `python` on `PATH`. It
synchronizes the hashed development lock into `.venv`. `make quality` runs the read-only
formatting, lock, type, dead-code, and whitespace checks; `make check` adds the full test suite.
`make test` runs that suite alone, and `make format` applies the repository formatters. Project
commands refuse a missing or outdated setup. Biome, Taplo, and shfmt are separate native tools on
`PATH` for formatting.

Use `make install` to install the command-line tool with `uv tool`, or `make reinstall` to replace
an existing installation. `make format-check`, `make lock-check`, `make type-check`,
`make dead-code-check`, and `make whitespace-check` run the corresponding read-only checks.

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs `quality` first. The `test` jobs then run in
parallel. `package` starts only after those jobs pass; `hosted-validation` reports the outcome of a
hosted run, including failures.

CI uses GitHub-hosted runners unless a run explicitly requests local runners. In a private
repository, the Actions variables `CI_LINUX_RUNNER`, `CI_MACOS_RUNNER`, and `CI_WINDOWS_RUNNER`
select local runners for runs started from Actions > CI > Run workflow with the hosted option
disabled. If a variable is absent, that operating system uses a hosted runner. Pushes, pull
requests, public repositories, and runs with the hosted option enabled use hosted runners for every
job and run every test.

A staging repository sets the Actions variable `CI_RUN_ON_PUSH` to `false`. Its pushes then appear
as `CI / push skipped` and run no jobs; CI runs only on request. A newer requested run on a branch
cancels an older one. Push runs are never cancelled, because the Release workflow needs the tagged
commit's push run.

| Requested run                                                 | Actions > CI > Run workflow | Title               |
| ------------------------------------------------------------- | --------------------------- | ------------------- |
| Quick: local runners, slow tests omitted on macOS and Windows | hosted off, slow tests off  | `CI / local`        |
| Full: local runners, every test                               | hosted off, slow tests on   | `CI / local (full)` |
| Hosted: GitHub-hosted runners, every test                     | hosted on                   | `CI / hosted`       |

The `test` matrix covers Python 3.13 to 3.15 on Linux and Python 3.13 on macOS and Windows. All
test jobs have a one-hour hang guard, on local and hosted runners, in quick and full runs.
`package` also loads the bundled plugin entry points from the installed wheel. The required native
POSIX and Windows containment tiers run separately in both modes, and CI rejects a tier with
skipped tests. The ordinary suite excludes those native tiers.

CI runs tests in parallel with pytest-xdist. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` when a runner's
visible CPU count exceeds its CPU quota. For the same scheduling locally, run:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

The manual [Release workflow](../.github/workflows/release.yml) publishes the package to PyPI after
checking its version tag and a successful push CI run for the tagged commit.

## Dependency updates

`requirements-ci.txt` pins and hashes development tools and build dependencies.
`requirements-test.txt` pins and hashes the smaller test environment, constrained by the same
versions in `requirements-ci.txt`. CI installs the development file in the quality job and the test
file in each test job. The package job builds distributions and installs the wheel in a clean
environment under the same build and runtime constraints.

After changing dependencies, regenerate both files with `make lock-update`. To refresh existing
compatible versions deliberately, run `make lock-update LOCK_FLAGS=--upgrade`. Review and commit
both generated files together. `make lock-check` checks their contents without rewriting them. Use
the uv version pinned in [the CI workflow](../.github/workflows/ci.yml) when regenerating pins.
Published dependency requirements in `pyproject.toml` remain version ranges; the pin files are for
CI.

## Native test tiers

Currently no tests carry the `slow` marker, so both modes select the same ordinary suite. The
required native POSIX and Windows containment tiers run separately in both modes, and CI rejects a
tier with skipped tests. The ordinary suite excludes those native tiers. Test jobs use the same
one-hour hang guard in every mode. Run the full local suite with `make test`.

The Markdown formatter uses Taplo for TOML code blocks and shfmt for shell code blocks. CI
downloads their official Linux binaries for the quality runner's architecture and verifies pinned
SHA-256 checksums. The versions and checksums live in the workflow. uv excludes `mdformat-config`'s
Taplo Python dependency because that distribution lacks Linux and Windows ARM wheels; the native
executable supplies the formatter instead. For local Markdown formatting, provide `taplo` and
`shfmt` on `PATH`. Test jobs do not need them.
