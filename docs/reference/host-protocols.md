# Host protocol reference

Every step reads the current native input object on stdin. Exit zero with empty stdout (including
ASCII-whitespace-only stdout) is neutral for all hosts. Unknown input fields survive accepted
replacements. Structured output must be one strict UTF-8 JSON object with no duplicate keys,
non-standard numbers, extra documents, or trailing non-whitespace.

## Structured final response schemas

The top-level and nested object boundaries below describe the wrapper's final structured output.
Optional members appear only when a step contributes a valid value that survives composition.

| Host        | Event       | Final alternatives                                                                            | Top-level members                                                                                                           | hookSpecificOutput members                                                                             | Coexistence and limits                                                                                                                                                                                                                                                                                                                                                             |
| ----------- | ----------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code | PreToolUse  | Empty stdout, native plain text when no structured result survives, or one structured object. | `continue`, `stopReason`, `suppressOutput`, `systemMessage`, `terminalSequence`, `hookSpecificOutput`                       | `hookEventName`, `permissionDecision`, `permissionDecisionReason`, `additionalContext`, `updatedInput` | Universal top-level fields and `hookSpecificOutput` may coexist. `continue` is emitted only as false, and `stopReason` requires it. `hookEventName` and `permissionDecision` are required in the nested object. `permissionDecisionReason` is optional. `additionalContext` and `updatedInput` are emitted only for `allow` or `ask`; `defer` omits both.                          |
| Claude Code | PostToolUse | Empty stdout, native plain text when no structured result survives, or one structured object. | `continue`, `stopReason`, `suppressOutput`, `systemMessage`, `terminalSequence`, `decision`, `reason`, `hookSpecificOutput` | `hookEventName`, `additionalContext`, `updatedToolOutput`                                              | Universal top-level fields, top-level `decision` and `reason`, and `hookSpecificOutput` may coexist. `decision` can only be `block`, and `reason` requires it. `continue` is emitted only as false, and `stopReason` requires it. The nested object requires `hookEventName` and may contain `additionalContext`, `updatedToolOutput`, or both.                                    |
| Codex       | PreToolUse  | Empty stdout or one structured object.                                                        | `systemMessage`, `hookSpecificOutput`                                                                                       | `hookEventName`, `permissionDecision`, `permissionDecisionReason`, `additionalContext`, `updatedInput` | Top-level `systemMessage` and `hookSpecificOutput` may coexist. The nested object requires `hookEventName`. `permissionDecision` is `allow` or `deny`; `permissionDecisionReason` requires a decision. `allow` requires `updatedInput` and `updatedInput` requires `allow`. A context-only object omits `permissionDecision`. `deny` omits `additionalContext` and `updatedInput`. |
| Codex       | PostToolUse | Empty stdout or one structured object.                                                        | `systemMessage`, `decision`, `reason`, `continue`, `stopReason`, `hookSpecificOutput`                                       | `hookEventName`, `additionalContext`                                                                   | All top-level groups and `hookSpecificOutput` may coexist. `decision` can only be `block`, and `reason` requires it. `continue` is emitted only as false, and `stopReason` requires it. The nested object requires `hookEventName` and carries `additionalContext`. Mutation fields are never emitted.                                                                             |
| Antigravity | PreToolUse  | One structured decision object.                                                               | `decision`, `reason`, `permissionOverrides`                                                                                 | None                                                                                                   | `decision` is always present and is `deny`, `force_ask`, `ask`, or `allow`. `reason` retains nonempty winning-child reasons for every decision, including `neutral` normalized to `allow`. Wrapper warnings are appended only for winning `ask`, `force_ask`, or `deny`. `permissionOverrides` is omitted for `deny`. An all-neutral chain emits `allow`.                          |
| Antigravity | PostToolUse | Exactly `{}`.                                                                                 | None                                                                                                                        | None                                                                                                   | No response groups coexist because the final object has no members.                                                                                                                                                                                                                                                                                                                |

## Claude Code

### PreToolUse

| Contract part    | Behavior                                                                                                                                     |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Minimum input    | `hook_event_name`, non-empty `tool_name`, and present `tool_input`                                                                           |
| Neutral response | Empty stdout                                                                                                                                 |
| Native decisions | `deny`, `defer`, `ask`, `allow`, plus global `continue: false`                                                                               |
| Precedence       | Global `continue: false` outranks the event decision; Claude Code documents `deny > defer > ask > allow`; neutral ranks below allow          |
| Mutation         | `updatedInput` replaces `tool_input`; final `allow` or `ask` may retain it                                                                   |
| Exits            | Exit 2 becomes terminal deny from bounded stderr; other non-zero exits report and continue                                                   |
| Malformed output | Claude Code 2.1.220 on macOS: after leading spaces, tabs, and line feeds, `{` selects whole-stdout JSON parsing; a failed parse is malformed |
| Field limits     | Composed strings and every string value nested in `updatedInput` are capped at 10,000 characters; final state is capped at 1 MiB             |
| Final shape      | Empty, native plain text, or the exact structured schema above                                                                               |

Claude Code owns `defer` eligibility for a current `-p` invocation. Universal fields include
`continue`, `stopReason`, `suppressOutput`, `systemMessage`, and `terminalSequence`. The first
terminal sequence wins. The deprecated top-level `decision` values `approve` and `block` are
accepted instead of a current pre-tool decision and normalize to nested `allow` and `deny`. Claude
Code 2.1.220 on macOS treats a native parse failure as nonblocking. The wrapper blocks malformed
output on this event by default.

An `updatedInput` string value over the limit makes the mutation undeliverable; the wrapper rejects
the replacement rather than truncating it. This pre-tool failure blocks even with
`--allow-on-failure`, so the original operation does not run without the requested replacement.

### PostToolUse

| Contract part    | Behavior                                                                                                                        |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Minimum input    | Pre-tool fields plus present `tool_response`                                                                                    |
| Neutral response | Empty stdout                                                                                                                    |
| Native decisions | `decision: block` and global `continue: false`                                                                                  |
| Precedence       | Either terminal control stops later steps                                                                                       |
| Mutation         | `updatedToolOutput` replaces `tool_response`; documented MCP form is normalized                                                 |
| Exits            | Exit 2 becomes terminal feedback; other non-zero exits report and continue                                                      |
| Malformed output | Same Claude Code 2.1.220 macOS parsing behavior as pre-tool                                                                     |
| Field limits     | Composed strings and every string value nested in a replacement are capped at 10,000 characters; final state is capped at 1 MiB |
| Final shape      | Empty, native plain text, or the exact structured schema above                                                                  |

The documented `updatedMCPToolOutput` input form is accepted only for an MCP tool and normalizes to
final `updatedToolOutput`. Returning both post-tool mutation forms is ambiguous and unsupported.
Every string value nested in `updatedToolOutput` or accepted `updatedMCPToolOutput` must fit the
limit. The wrapper rejects rather than truncates an undeliverable replacement, reports it, and
retains the previous payload. Blocking feedback cannot undo the completed tool action.

## Codex

### PreToolUse

| Contract part    | Behavior                                                                      |
| ---------------- | ----------------------------------------------------------------------------- |
| Minimum input    | `hook_event_name`, non-empty `tool_name`, and present `tool_input`            |
| Neutral response | Empty stdout                                                                  |
| Native decisions | Neutral, `deny`, context, and `allow` with replacement                        |
| Precedence       | Deny is terminal; the wrapper retains all ordered reports                     |
| Mutation         | Object `updatedInput` replaces `tool_input`; tool-specific validation applies |
| Exits            | Exit 2 becomes terminal deny; other non-zero exits report and continue        |
| Malformed output | Unparsable leading `{` or `[` is malformed; other text is neutral             |
| Field limits     | Final state is capped at 1 MiB; diagnostics are bounded                       |
| Final shape      | Empty or the exact structured schema above                                    |

Codex documents decision meanings but no precedence for disagreeing hooks; terminal deny behavior
is a wrapper contract. `allow` without a valid replacement and `ask` are unsupported. The
deprecated top-level `decision: block` form normalizes to nested `permissionDecision: deny`; it
cannot coexist with a current nested decision. A replacement for `Bash` or `apply_patch` requires a
string `command` member. Other local and MCP tools accept an arguments object without that extra
field requirement.

### PostToolUse

| Contract part    | Behavior                                                                   |
| ---------------- | -------------------------------------------------------------------------- |
| Minimum input    | Pre-tool fields plus present `tool_response`                               |
| Neutral response | Empty stdout                                                               |
| Native decisions | `decision: block`, `continue: false`, context, and messages                |
| Precedence       | Either terminal control stops later steps                                  |
| Mutation         | None                                                                       |
| Exits            | Exit 2 becomes terminal feedback; other non-zero exits report and continue |
| Malformed output | Unparsable leading `{` or `[` is malformed; other text is neutral          |
| Field limits     | Final state is capped at 1 MiB; diagnostics are bounded                    |
| Final shape      | Empty or the exact structured schema above                                 |

Codex documents no post-tool mutation. Mutation fields are unsupported, not ignored.

## Antigravity

### PreToolUse

| Contract part    | Behavior                                                                                                                                                                          |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Minimum input    | Integer `stepIdx`, `toolCall` object, non-empty `toolCall.name`, object `toolCall.args`                                                                                           |
| Neutral response | All-neutral chain emits `decision: allow`                                                                                                                                         |
| Native decisions | Current host documentation lists `allow`, `ask`, `deny`, `force_ask`, and `deny_unless_prior_grant`; the wrapper accepts child `allow`, `ask`, `deny`, `force_ask`, and `neutral` |
| Precedence       | Wrapper contract: `deny > force_ask > ask > allow > neutral`                                                                                                                      |
| Mutation         | None; equal winning `permissionOverrides` survive and conflicting arrays deny                                                                                                     |
| Exits            | Any non-zero exit is an untrustworthy failure and fails closed by default                                                                                                         |
| Malformed output | Every unparsable output is malformed; `{}` or an object without a decision is malformed                                                                                           |
| Field limits     | Final state is capped at 1 MiB                                                                                                                                                    |
| Final shape      | The exact structured decision schema above, terminated by one line feed                                                                                                           |

Current Antigravity host documentation lists five native pre-tool decision values: `allow`, `ask`,
`deny`, `force_ask`, and `deny_unless_prior_grant`. The wrapper accepts child `allow`, `ask`,
`deny`, `force_ask`, and `neutral` decisions; `neutral` carries no permission decision. If a native
hook emits `deny_unless_prior_grant`, the wrapper receives that output and classifies it as
malformed. The existing PreToolUse failure policy converts the malformed output to an unconditional
fail-closed denial. An all-neutral chain still emits the synthetic final `allow`, which grants real
automatic permission.

### PostToolUse

| Contract part    | Behavior                                                                       |
| ---------------- | ------------------------------------------------------------------------------ |
| Minimum input    | Integer `stepIdx`                                                              |
| Applicability    | Explicit `toolCall: null` starts no steps; absent or non-null values run them  |
| Neutral response | Empty stdout or `{}`                                                           |
| Native decisions | None                                                                           |
| Precedence       | No decision composition                                                        |
| Mutation         | None                                                                           |
| Exits            | Non-zero exits report and later steps continue                                 |
| Malformed output | Unparsable nonempty output is malformed; a nonempty JSON object is unsupported |
| Field limits     | Final state is capped at 1 MiB                                                 |
| Final shape      | Exactly `{}` plus one line feed                                                |

Antigravity documents no independent feedback field. Post-tool failures remain in developer
diagnostics. Explicit `toolCall: null` callbacks are inapplicable, so the wrapper starts no child
and emits exact `{}` plus one line feed. The attributable object-shaped callback still runs the
chain. The wrapper does not infer applicability from callback order or retain cross-callback state.

See the [CLI limits](cli.md#limits) and [concepts](../concepts.md).
