# Compatibility

sequential-hooks runs on Linux, macOS, and native Windows 10 or later. See
[platform behavior](platforms.md) for runtime requirements and process handling.

## Supported hosts

The built-in adapters chain `PreToolUse` and `PostToolUse` command hooks for:

- [Claude Code](../plugins/claude/README.md)
- [Codex](../plugins/codex/README.md)
- [Antigravity](../plugins/agy/README.md)

Each setup guide covers registration, supported responses, and host-specific limitations. See the
[hook protocol reference](reference/host-protocols.md) for the accepted input and output contracts.

Run `sequential-hooks doctor --agent NAME` after installation or configuration changes. It checks
registration, static configuration, and local platform capability. It does not execute hooks or
establish live-host compatibility.

## Antigravity limitations

A post-tool payload with an explicit `toolCall: null` is treated as inapplicable: the wrapper
starts no child steps and returns the native empty response. An absent or non-null `toolCall` still
runs the chain.

An all-neutral pre-tool chain emits `allow`, which grants automatic permission. If the tool call
must prompt, include a step that returns `ask` or `force_ask`, or do not register that chain.

The Antigravity adapter rejects a successful child response containing only `systemMessage`. On
`PreToolUse`, it stops the chain with a deny decision by default. On `PostToolUse`, it reports the
failure and continues. A hook that uses that response as its success receipt cannot be composed in
an Antigravity chain until the child response and adapter contract agree.

See the [Antigravity setup guide](../plugins/agy/README.md) for registration details and host
limitations.
