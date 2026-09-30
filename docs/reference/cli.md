# CLI reference

## Hook grammar

```text
usage: sequential-hooks (-a|--agent) NAME [-e|--event EVENT] [--step-timeout SECONDS] [--allow-on-failure] STEP [ARG ...] [-- STEP [ARG ...]] ...
```

The wrapper accepts one through three non-empty steps. Wrapper options must precede the first step.
The exact `--` token separates steps wherever it appears and is unavailable to a child. No escape
form passes that token through.

On invalid wrapper syntax, only options parsed before the first error can identify the agent and
event for a configuration response. Put `--agent` first and, for Antigravity, `--event` before any
other wrapper options. The parser does not search later arguments for missing context.

`-h, --help` prints hook-mode help without reading stdin. `doctor -h` and `doctor --help` print
doctor help with the same short and long option spellings.

`-a, --agent` is required and selects a coding agent by its installed host-adapter entry-point
name; the built-in names are `claude`, `codex`, and `agy`. `-e, --event` selects an explicit event.
Claude Code and Codex provide the event through stdin and reject it. Antigravity requires exactly
one event with `PreToolUse` or `PostToolUse`. Event names are case-sensitive. See
[adding a host](../adding-a-host.md) for third-party adapters.

`--step-timeout` accepts one positive finite number of seconds. It resets for each step and is
absent by default. Set the host's outer timeout above the whole chain budget.

Example:

```text
sequential-hooks --agent codex --step-timeout 30 defsort hook --agent codex -- docwrap hook --agent codex
```

The two child `--agent codex` pairs belong to `defsort` and `docwrap`. The wrapper does not pass
down or otherwise inherit its own `--agent` setting.

## Failure override

On pre-tool events, `--allow-on-failure` changes these failures from block to report-and-continue:
not found, spawn, crash, timeout, stdout limit, output-worker join timeout, malformed output,
unsupported output, oversized output, unavailable containment, and an unsupported executable. When
a step never starts, the wrapper skips it. Post-tool already reports these failures and continues.

The flag does not weaken a valid native result, accept invalid host input, accept an event
mismatch, relax an undeliverable decision-bearing mutation, override an interrupt, or recover an
unexpected wrapper invariant failure.

## Limits

| Resource                                    | Limit             |
| ------------------------------------------- | ----------------- |
| Steps per hook                              | 3                 |
| Stdout per step                             | 1 MiB             |
| Serialized final response state             | 1 MiB             |
| Forwarded stderr per step                   | 1 MiB             |
| Forwarded stderr per chain                  | 4 MiB             |
| Claude Code composed text field             | 10,000 characters |
| Claude Code nested replacement string value | 10,000 characters |

Byte limits apply before decoding. Crossing the stdout limit terminates the step. Crossing a stderr
limit writes one truncation notice, keeps draining, and does not terminate a healthy step. Final
size reduction discards optional text before reasons and never discards a decision-bearing mutation
to permit the original operation. The Claude Code replacement limit applies to every string value
nested in a replacement and is separate from the composed-text and final-response limits.

## Output and exits

A step exchanges native JSON bytes with the selected host adapter. The wrapper writes only the
single final native response to stdout. Step stderr is labeled:

```text
sequential-hooks: step 2 (second-hook): message
```

Top-level `--help` and `--version` do not read stdin. Help exits zero and writes to stdout. Version
writes the installed distribution version to stdout and exits zero. If distribution metadata is
unavailable, version writes a diagnostic to stderr and exits 2 without stdout. Hook-mode stdout
carries only the host response. `doctor --help` and doctor reports also write text to stdout.
Closed output pipes are absorbed without changing the command's exit status. Hook mode follows the
native host response contract; handled native outcomes normally exit zero. A forwarded signal exits
with 128 plus the signal number.

Failures follow three tiers. A functioning adapter translates child failures into its native
response. A configuration error with a loaded host and a trusted event, including an invalid chain
definition, a rejected `--event`, or invalid native input, is answered by that host's generic
configuration response and returns its host-owned exit code; the built-in responses exit zero. That
response uses the `spawn` category and does not include the specific syntax or input failure.
Developer stderr supplies bounded diagnostic detail. When no functioning host exists, because
`--agent` names an unknown, duplicated, unloadable, or outdated adapter, the required `--event` is
missing or not served, malformed JSON prevents reading a payload event, a valid payload names no
served event, the adapter malfunctions, or the host's configuration response itself fails, the
wrapper writes nothing to stdout, one diagnostic line to stderr, and exits 2. Claude Code and Codex
treat that exit as blocking; Antigravity does not.

`doctor` has a separate grammar:

```text
sequential-hooks doctor (-a|--agent) NAME
```

For example, run `sequential-hooks doctor --agent claude` or its short equivalent,
`sequential-hooks doctor -a claude`.

Reports list configuration problems first, keep each hook's findings together in registration
order, and finish with cross-hook findings.

It exits 0 when findings are informational, 1 when at least one warning or error needs action, and
2 for invalid invocation. Any agent name is accepted; an adapter that cannot be loaded is reported
as the `host_unavailable` finding rather than an invocation error.

The `configuration sources:` section lists fixed candidate files and discovered drop-in files in
inspection order. It does not list every directory examined during discovery. An unreadable drop-in
directory can appear as the diagnostic source that owns its finding.

## Doctor report escapes

The report begins with `display: ASCII backslash escapes`. A displayed `\\` represents one literal
backslash. Control characters and non-ASCII code points use Python-style backslash escapes. Command
values are bounded and secret-looking option values are redacted. `doctor` reads configuration and
static platform capability only; it does not execute, edit, install, or grant trust.
