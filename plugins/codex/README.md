# Codex

Use sequential-hooks for Codex `PreToolUse` and `PostToolUse` command hooks. Codex discovers
`hooks.json` and inline `[hooks]` tables in `config.toml` at these local paths:

- User: `~/.codex/hooks.json` and `~/.codex/config.toml`.
- System: `/etc/codex/hooks.json` and `/etc/codex/config.toml`.
- Each project layer: `.codex/hooks.json` and `.codex/config.toml`.

sequential-hooks has no configuration file.

Official reference: [Codex hooks](https://learn.chatgpt.com/docs/hooks).

## Register hooks

Choose a matcher that covers only the tools handled by every step. Keep one wrapper for each event
and matcher, and remove separately registered copies of its steps.

The child commands are examples. Replace `first-hook`, `second-hook`, and `audit-hook` with
installed hooks that accept the corresponding Codex event payload.

Codex configuration (`~/.codex/hooks.json`, pre-tool):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|apply_patch",
        "hooks": [
          {
            "type": "command",
            "command": "sequential-hooks --agent codex --step-timeout 30 first-hook hook -- second-hook hook",
            "commandWindows": "sequential-hooks --agent codex --step-timeout 30 first-hook hook -- second-hook hook",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

Codex configuration (`~/.codex/config.toml`, post-tool):

```toml
[features]
hooks = true

[hooks]
PostToolUse = [
  { matcher = "Bash|apply_patch", hooks = [
    { type = "command", command = "sequential-hooks --agent codex --step-timeout 30 audit-hook after", command_windows = "sequential-hooks --agent codex --step-timeout 30 audit-hook after", timeout = 600 },
  ] },
]
```

JSON uses `commandWindows`. TOML accepts either `command_windows` or `commandWindows`; use one
spelling, never both. The override lets Codex own Windows tokenization explicitly. The outer Codex
launcher and the wrapper's inner step launcher are separate boundaries. Inner steps always use
literal argument vectors without a shell.

On POSIX, `doctor` tokenizes only literal command text within its conservative static grammar. It
reports dynamic shell syntax as `command_dynamic` without evaluating it. On Windows, Codex still
launches the portable command or `commandWindows` through the Windows shell, but `doctor` does not
parse that shell text; the registration reports `windows_shell` and `command_uninspectable`.

Codex command hooks currently default to 600 seconds. Set the outer timeout above the total
worst-case duration. Each `--step-timeout` resets for the next step.

## Decisions and output

Pre-tool outcomes are neutral, deny, context, and allow with a replacement. Codex has no pre-tool
`ask`. A deny is terminal. An allow must carry a valid `updatedInput`; a step with no replacement
stays silent.

Post-tool supports block, global stop, context, and messages but no mutation. A post-tool block
stops later steps and cannot undo a completed action. For exit-zero unparsable stdout, text whose
first non-whitespace character is `{` or `[` is malformed; other text is neutral and remains in
developer diagnostics.

## Trust and verify

Codex requires review of every non-managed hook. Open `/hooks`, inspect the exact definition, and
approve it through Codex's trust flow. `doctor` reports registrations from the configuration files
it can inspect but does not grant trust.

```text
sequential-hooks doctor --agent codex
```

Common findings:

- `host_missing`: add `--agent codex` to the wrapper.
- `wrong_host`: change the wrapper to `--agent codex`.
- `event_forbidden`: use this configured agent and remove `--event`; the host supplies
  `hook_event_name` on stdin.
- `outer_timeout_only`: add `--step-timeout` if each child needs an independent bound.
- `codex_project_trust_unknown` or `codex_hook_trust_unknown`: review the hook with `/hooks`.
- `codex_windows_command_ambiguous`: keep only one TOML Windows override spelling.
- `codex_windows_posix_command`: add `commandWindows` and review the hook with `/hooks`.
- `duplicate_wrapper`: keep one wrapper for each event and matcher.
- `duplicate_child_registration`: remove the separately registered child.
- `child_executable_missing`: install or configure the literal child executable.

`doctor` cannot establish active profiles, session overrides, plugin hooks, persisted trust, cloud
or MDM hooks, managed `requirements.toml` constraints, or the effect of `allow_managed_hooks_only`.

The trust warnings persist after approval through `/hooks` because `doctor` cannot read those saved
decisions. These warnings keep its exit status at 1; they do not mean that approval failed or that
you must approve the hook again.

## Configuration failures

A configuration error with a trusted event, including invalid wrapper syntax, a rejected `--event`,
or invalid native input, is answered with a generic `spawn` report: a `PreToolUse` deny or a
`PostToolUse` block. Developer stderr gives bounded detail. `--agent` must precede the first syntax
error for that response. If the adapter cannot be loaded, stdin is malformed JSON, or the payload
does not identify a served event, the wrapper may instead exit 2 with one diagnostic line and no
response; Codex treats that exit as blocking.

See the [protocol reference](../../docs/reference/host-protocols.md#codex) and
[troubleshooting guide](../../docs/troubleshooting.md).
