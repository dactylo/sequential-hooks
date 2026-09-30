# sequential-hooks

`sequential-hooks` runs coding-agent hooks in a strict order. Without a wrapper, a host may start
several hooks concurrently or in an undocumented order. A validator can then inspect an old tool
input while another hook is still replacing it. Register one wrapper hook and place the former hook
commands inside it as ordered steps.

## Quick start

`sequential-hooks` requires native CPython 3.13 or newer and `uv`. The project is not published to
a package index. Install the current checkout from the repository root:

```text
uv tool install .
```

To update an existing installation after pulling changes:

```text
uv tool install --reinstall .
```

sequential-hooks has no configuration file. Choose the coding agent that will run the wrapper, then
add the registration from its setup guide:

- [Claude Code](plugins/claude/README.md)
- [Codex](plugins/codex/README.md)
- [Antigravity](plugins/agy/README.md)

Check [compatibility](docs/compatibility.md) for the recorded host versions and known host limits,
and [platforms](docs/platforms.md) for process handling. The guides use `first-hook`,
`second-hook`, and `audit-hook` as sample child commands. Replace them with hooks you have
installed and want to run in order. Move those child hooks into the wrapper and remove their
separate registrations, or the host may still start duplicate copies concurrently.

The wrapper command expects a native host payload on stdin. Configure it as a host hook rather than
running the hook command interactively. After saving the registration, inspect it with the matching
read-only report:

```text
sequential-hooks doctor --agent claude
sequential-hooks doctor --agent codex
sequential-hooks doctor --agent agy
```

Exit 0 means the findings are informational. Exit 1 means at least one warning or error needs
action. The [troubleshooting guide](docs/troubleshooting.md) explains common findings.

Antigravity requires an explicit event in each hook command. An all-neutral Antigravity pre-tool
chain emits the host's real automatic `allow` decision; it does not preserve the ordinary
permission prompt. Review the [Antigravity setup guide](plugins/agy/README.md) before enabling it.

## How commands compose

`-a, --agent` selects the installed host adapter for a coding agent. The built-in names are
`claude`, `codex`, and `agy`; a third-party adapter installed with
`uv tool install . --with PACKAGE` is selected by its own name. Nothing is inferred from the
payload. See [adding a host](docs/adding-a-host.md).

`-h, --help` prints help for wrapper mode; `doctor -h` and `doctor --help` print doctor help.

```text
sequential-hooks --agent codex --step-timeout 30 defsort hook --agent codex -- docwrap hook --agent codex
```

The exact `--` token separates steps and cannot be passed to a step. Each child receives its own
literal arguments, including its own `--agent`; wrapper options are not inherited by child
commands.

Continue with the [documentation map](docs/README.md), [concepts](docs/concepts.md),
[security](docs/security.md), or the [CLI reference](docs/reference/cli.md).
