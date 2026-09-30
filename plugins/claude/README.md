# Claude Code

Use `sequential-hooks` for Claude Code `PreToolUse` and `PostToolUse` command hooks. Claude Code
owns these local files:

- User: `~/.claude/settings.json`.
- Project: `.claude/settings.json`.
- Local project: `.claude/settings.local.json`.
- macOS managed: `/Library/Application Support/ClaudeCode/managed-settings.json` and JSON files in
  `/Library/Application Support/ClaudeCode/managed-settings.d/`.
- Linux managed: `/etc/claude-code/managed-settings.json` and JSON files in
  `/etc/claude-code/managed-settings.d/`.
- Windows managed: `C:\Program Files\ClaudeCode\managed-settings.json` and JSON files in
  `C:\Program Files\ClaudeCode\managed-settings.d\`.

`sequential-hooks` has no configuration file.

Official references: [hooks](https://code.claude.com/docs/en/hooks) and
[hooks guide](https://code.claude.com/docs/en/hooks-guide).

## Register hooks

Use Claude Code's direct exec form: `command` names the executable and `args` contains its literal
arguments. The example limits the hook to selected tools. Choose a matcher that covers only the
tools handled by every step. Move the child hooks into the wrapper and remove their separate
registrations, or Claude Code may still start duplicate copies concurrently.

The child commands are examples. Replace `first-hook`, `second-hook`, and `audit-hook` with
installed hooks that accept the corresponding Claude Code event payload.

Claude Code configuration (`~/.claude/settings.json`, pre-tool):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "sequential-hooks",
            "args": [
              "--agent",
              "claude",
              "--step-timeout",
              "30",
              "first-hook",
              "hook",
              "--",
              "second-hook",
              "hook"
            ],
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

Claude Code configuration (`~/.claude/settings.json`, post-tool):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "sequential-hooks",
            "args": [
              "--agent",
              "claude",
              "--step-timeout",
              "30",
              "audit-hook",
              "after"
            ],
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

Without `args`, Claude Code sends the command string to `sh -c` on macOS and Linux. On Windows it
uses Git Bash when available and otherwise PowerShell. This shell behavior belongs only to the
command-string alternative. The direct exec form removes that outer shell on every platform. The
wrapper always starts its inner steps as literal argument vectors without a shell.

Set the outer `timeout` above the worst-case chain duration. A chain of three steps with
`--step-timeout 30` can use about 90 seconds plus startup and cleanup. The wrapper has no internal
timeout unless you set one.

## Decisions and output

Claude Code supplies pre-tool precedence: `deny > defer > ask > allow`. The wrapper preserves that
ordering. Claude Code alone decides whether `defer` is eligible for the current `-p` invocation;
the wrapper and `doctor` do not predict it.

A silent step is neutral. A valid pre-tool replacement reaches the next step. A post-tool block
stops later steps but cannot undo the completed tool action.

Claude Code 2.1.220 on macOS removes leading spaces, tabs, and line feeds before choosing the
output parser. If the first remaining byte is `{`, it parses the complete stdout value as one JSON
document. Malformed, multiline, and mixed results on that path are nonblocking native parse errors.
Otherwise the complete output is native plain text, even when a later line begins with `{`. The
wrapper applies its own documented parser classification and failure policy.

## Trust and verify

Review the exact command and every inner step before enabling the hook. Hooks run with your user
authority before Claude Code permits the proposed tool call.

```text
sequential-hooks doctor --agent claude
```

`doctor` inspects the listed user, project, local project, and managed configuration files. It
cannot establish server-managed settings, macOS managed preferences or plist delivery, Windows
policy registry delivery, or plugin-provided hooks.

The reported enabled state comes from the configuration files and activation gates `doctor` can
inspect and model. Claude Code has no per-hook `enabled` field. `doctor` applies the visible
top-level `disableAllHooks` value with managed, local, project, and user precedence. Claude Code
has an additional `allowManagedHooksOnly` activation gate that `doctor` does not inspect or model.
Claude Code may therefore decline an unmanaged hook that `doctor` reports as enabled. Unavailable
managed delivery and plugin gates remain listed as limitations rather than turning every visible
registration into a warning.

Common findings:

- `wrong_host`: change the wrapper to `--agent claude`.
- `event_forbidden`: use this configured agent and remove `--event`; the host supplies
  `hook_event_name` on stdin.
- `duplicate_wrapper`: keep one wrapper for each event and matcher.
- `duplicate_child_registration`: remove the separately registered child.
- `outer_timeout_missing` or `outer_timeout_too_small`: set an outer timeout above the chain
  budget.
- `command_dynamic` or `command_uninspectable`: use the documented direct exec form. For rejected
  wrapper arguments, check the [CLI grammar](../../docs/reference/cli.md#hook-grammar).
- `child_executable_missing`: install or configure the literal child executable.

## Configuration failures

A configuration error with a trusted event, including invalid wrapper syntax, a rejected `--event`,
or invalid native input, is answered with a generic `spawn` report: a `PreToolUse` deny or a
`PostToolUse` block. Developer stderr gives bounded detail. `--agent` must precede the first syntax
error for that response. If the adapter cannot be loaded, stdin is malformed JSON, or the payload
does not identify a served event, the wrapper may instead exit 2 with one diagnostic line and no
response; Claude Code treats that exit as blocking.

See the [protocol reference](../../docs/reference/host-protocols.md#claude-code) and
[security guide](../../docs/security.md).
