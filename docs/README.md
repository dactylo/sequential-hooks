# Documentation

Start with the project [quick start](../README.md#quick-start). It installs the current checkout,
routes you to the correct host setup guide, and shows how to verify the saved registration.

## Get started

- [Claude Code](../plugins/claude/README.md): register pre-tool and post-tool command hooks.
- [Codex](../plugins/codex/README.md): configure JSON or TOML hooks and complete the trust review.
- [Antigravity](../plugins/agy/README.md): register explicit events and understand automatic
  `allow`.
- [Troubleshooting](troubleshooting.md): interpret `doctor` findings and runtime failures.

## Understand the behavior

- [Concepts](concepts.md): ordering, replacements, decisions, and diagnostics.
- [Security](security.md): trust boundaries and pre-permission execution.
- [Platforms](platforms.md): process containment on POSIX and native Windows.
- [Compatibility](compatibility.md): recorded host CLI versions and known host limits.

## Reference

- [CLI reference](reference/cli.md): grammar, limits, exits, and failure overrides.
- [Host protocol reference](reference/host-protocols.md): accepted input, output, decisions, and
  mutations for every event.

## Contribute

- [Development](development.md): run checks and update the pinned CI dependencies.
- [Adding a host](adding-a-host.md): the public adapter contract, the composition skeleton,
  registration, and evidence.
