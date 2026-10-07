# Antigravity

The Antigravity adapter chains `PreToolUse` and `PostToolUse` hooks. Antigravity may invoke an
exact post-tool matcher for extra callbacks whose `toolCall` is explicitly null. sequential-hooks
treats that exact shape as inapplicable, starts no child step, and returns the native empty
response.

Antigravity CLI 1.1.13 documents five native hook events: `PreToolUse`, `PostToolUse`,
`PreInvocation`, `PostInvocation`, and `Stop`. The wrapper chains only `PreToolUse` and
`PostToolUse`.

Antigravity configuration may appear in `~/.gemini/config/hooks.json` or project
`.agents/hooks.json`. sequential-hooks has no configuration file.

Official reference: [Antigravity hooks](https://antigravity.google/docs/hooks).

## Registration

Antigravity uses a top-level map from hook names to event configurations. A top-level hook named
`hooks` follows the same schema as any other name; it is not a second wrapper shape.

The child commands are examples. Replace `first-hook` and `second-hook` with installed hooks that
accept the corresponding Antigravity event payload.

Antigravity configuration:

```json
{
  "sequential-hooks": {
    "enabled": true,
    "PreToolUse": [
      {
        "matcher": "run_command",
        "hooks": [
          {
            "type": "command",
            "command": "sequential-hooks --agent agy --event PreToolUse --step-timeout 5 first-hook hook -- second-hook hook",
            "timeout": 30
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "run_command",
        "hooks": [
          {
            "type": "command",
            "command": "sequential-hooks --agent agy --event PostToolUse --step-timeout 5 first-hook hook -- second-hook hook",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Each event registration must carry its exact event because Antigravity's payload does not identify
it. The wrapper commands are:

```text
sequential-hooks --agent agy --event PreToolUse --step-timeout 5 first-hook hook -- second-hook hook
sequential-hooks --agent agy --event PostToolUse --step-timeout 5 first-hook hook -- second-hook hook
```

An all-neutral pre-tool chain emits `allow`. That is Antigravity's real automatic permission, not a
neutral response. If the tool call must prompt, include a step that returns `ask` or `force_ask`,
or do not register this chain.

Use a matcher that covers only tools handled by every step. Remove separate registrations of those
steps. Antigravity otherwise remains free to start the duplicate copies concurrently.

Antigravity currently documents a 30-second command-handler default. Set an explicit outer timeout
above the chain budget; three steps at five seconds need more than 15 seconds for startup and
cleanup. Antigravity 1.1.8 starts the outer command with `sh -c` on macOS and Linux and `cmd /c` on
Windows. It expands `~` and uses the directory containing `hooks.json` as the working directory.
The wrapper starts each inner step as a literal argument vector without a shell.

## Decisions and output

Current Antigravity host documentation lists five native pre-tool decision values: `allow`, `ask`,
`deny`, `force_ask`, and `deny_unless_prior_grant`. The wrapper accepts child `allow`, `ask`,
`deny`, `force_ask`, and `neutral` decisions; `neutral` carries no permission decision and an
all-neutral chain normalizes to final `allow`. If a native hook emits `deny_unless_prior_grant`,
the wrapper receives that output and classifies it as malformed. The existing PreToolUse failure
policy converts the malformed output to an unconditional fail-closed denial.

The wrapper's pre-tool precedence is `deny > force_ask > ask > allow > neutral`; Antigravity
documents the meanings but no ordering for disagreeing hooks. A deny is terminal. Equal winning
`permissionOverrides` survive; conflicting winning arrays fail closed.

The wrapper accepts silence, ASCII-whitespace-only stdout, or `{}` from a post-tool step and emits
`{}`. Unparsable nonempty output is malformed; a nonempty JSON object is unsupported. A post-tool
failure is reported to developer diagnostics and later steps continue because Antigravity has no
independent feedback field.

Antigravity CLI 1.1.8 on macOS may emit two extra `PostToolUse` callbacks with explicit
`toolCall: null` around the attributable object-shaped callback. The wrapper still requires an
integer `stepIdx`, but it starts no child for the explicit-null shape. It emits exact `{}` plus one
line feed, writes no diagnostic, and exits `0`. A missing `toolCall` or any non-null value still
runs the configured chain. The wrapper does not deduplicate by callback order, `stepIdx`, or
conversation state.

Controlled Antigravity CLI 1.1.8 probes confirmed project registration loading and a pre-tool
handler on macOS. They did not establish live Linux or Windows event behavior. A separate macOS
post-tool probe found a matcher dispatch divergence, so required post-tool chaining still needs a
provider fix and a fresh live probe. The wrapper's post-tool response contract above applies when
the host invokes it.

## Trust and verify

Treat the wrapper and every inner step as trusted code with your user authority.

```text
sequential-hooks doctor --agent agy
```

`doctor` parses the named-hook map without executing commands. Static inspection can tokenize safe
POSIX shell text. Windows shell commands remain uninspectable because `doctor` does not evaluate
`cmd /c` syntax. The report's enabled state follows each named hook's `enabled` value; absent
`enabled` defaults to true, and a non-Boolean value is reported as malformed.

Common corrections:

- `event_missing`: add the registered event, `--event PreToolUse` or `--event PostToolUse`, to the
  active command.
- `wrong_event`: change the explicit event to the registered event.
- `wrong_host`: change the wrapper to `--agent agy`.
- `agy_chain_budget_uncontained`: set the outer timeout above the configured chain budget.
- `duplicate_wrapper`: keep one wrapper for each event and matcher.
- `duplicate_child_registration`: remove the separately registered child.

## Configuration failures

When `--agent agy` and a served `--event` precede the first syntax error, a configuration error,
including invalid wrapper syntax or invalid native input, is answered with a generic `spawn`
report: top-level `deny` on `PreToolUse`, or `{}` plus a developer diagnostic on `PostToolUse`. The
latter does not block. Without a valid `--event`, or when the adapter itself cannot be loaded, the
wrapper exits 2 with one diagnostic line and no response. Antigravity has no exit-code tier, so
that last resort does not block; it runs no step and grants nothing. Run `doctor` to catch a
missing or unsupported event and an unavailable adapter before a hook fires.

See the [protocol reference](../../docs/reference/host-protocols.md#antigravity) and
[security guide](../../docs/security.md).
