# Compatibility

Choose a host setup guide from the [quick start](../README.md#quick-start). Sequential-hooks runs
on Linux, macOS, and native Windows 10 or later; see [platform behavior](platforms.md) for runtime
requirements and process handling.

## Host adapters

The built-in adapters implement the hook protocols of these application and CLI versions:

| Host        | Version |
| ----------- | ------- |
| Claude Code | 2.1.220 |
| Codex       | 0.145.0 |
| Antigravity | 1.1.8   |

Run `sequential-hooks doctor --agent NAME` after installation to check registration, static
configuration, and local platform capability. Each host's setup guide explains how to register its
adapter and compose hooks.

## Known host limits

Antigravity 1.1.8 on macOS can send extra `PostToolUse` callbacks with an explicit
`toolCall: null`. The wrapper skips child steps for that shape; absent or non-null `toolCall`
values still run the chain. An all-neutral Antigravity pre-tool chain emits a real automatic
`allow` decision. Read the [Antigravity setup guide](../plugins/agy/README.md) before enabling one.

The sequential-hooks Antigravity adapter rejects a successful child response containing only
`systemMessage`. On `PreToolUse`, it stops the chain with a deny decision by default; on
`PostToolUse`, it reports the failure and continues. The current standalone defsort and docwrap
Antigravity hooks emit that shape as a success receipt, so they cannot be used as children in an
Antigravity chain. Do not register that combination until the child response and adapter contract
agree. The [Claude Code](../plugins/claude/README.md) and [Codex](../plugins/codex/README.md)
guides show their own composition examples.
