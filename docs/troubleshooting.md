# Troubleshooting

Start with the coding agent's read-only report:

```text
sequential-hooks doctor --agent claude
```

Doctor help and reports go to stdout. Invalid doctor invocations report diagnostics on stderr.

Use `codex` or `agy` for the other agents. Exit 0 means findings are informational. Exit 1 means at
least one warning or error is present. Exit 2 means the `doctor` invocation is invalid.

Codex trust warnings persist after host-side review because `doctor` cannot inspect saved trust
decisions. They keep the exit status at 1 even when the hook has been approved through `/hooks`.

## Registration findings

| Finding                                              | Correction                                                                                                                                                                                     |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `host_unavailable`                                   | Install the adapter package that registers this name in the `sequential_hooks.hosts` entry-point group, or fix its version.                                                                    |
| `enabled_unknown`                                    | Reconcile conflicting or malformed activation settings in the inspected configuration files, following the report's correction.                                                                |
| `wrong_host` or `host_missing`                       | Add the explicit agent name that owns the configuration file.                                                                                                                                  |
| `event_missing` or `wrong_event`                     | For Antigravity, use the exact registered `PreToolUse` or `PostToolUse` event.                                                                                                                 |
| `event_forbidden`                                    | Use the agent that owns the configuration and remove `--event`; Claude Code and Codex supply the event on stdin.                                                                               |
| `native_event_unsupported`                           | Wrapper registered under a native event it cannot serve. Register the wrapper only under `PreToolUse` or `PostToolUse`.                                                                        |
| `duplicate_wrapper`                                  | Keep one wrapper hook for each event and matcher.                                                                                                                                              |
| `definite_matcher_overlap`                           | Make same-event matcher scopes disjoint.                                                                                                                                                       |
| `duplicate_child_registration`                       | Remove the separately registered command already present as a step.                                                                                                                            |
| `command_dynamic` or `command_uninspectable`         | Use the host's documented literal launch form; for rejected wrapper arguments, check the [CLI grammar](reference/cli.md#hook-grammar). On Windows, check remote or device commands separately. |
| `outer_timeout_missing` or `outer_timeout_too_small` | Set the outer handler timeout above the worst-case chain budget and cleanup time.                                                                                                              |
| `child_executable_missing`                           | Install or configure the literal child executable.                                                                                                                                             |
| `wrapper_executable_missing`                         | Install the current checkout with `uv tool install .` or correct `PATH`.                                                                                                                       |
| `child_executable_unsupported`                       | Replace the Windows shim with a real `.exe` or `.com` executable entry point.                                                                                                                  |

Do not hide an unsupported Windows `.bat`, `.cmd`, or `.ps1` step behind a shell wrapper. The
wrapper requires a real executable because generic batch argument reconstruction is unsafe.

If Codex reports hooks it does not manage, review the active registrations with `/hooks` and remove
any duplicate child registrations. For Antigravity event and output behavior, see the
[host protocol](reference/host-protocols.md#antigravity). Check supported hosts and known limits in
[compatibility](compatibility.md), and process handling in [platforms](platforms.md). `doctor`
reports visible enabled state and static findings; a clean report is not live-host verification. On
Windows, remote or device executable paths are not resolved during static inspection; use trusted
configuration and verify those commands separately.

## Runtime failure categories

The implementation stores stable category IDs. Developer stderr replaces underscores with spaces:

| Internal category ID     | Displayed phrase         | Correction                                                                                               |
| ------------------------ | ------------------------ | -------------------------------------------------------------------------------------------------------- |
| `not_found`              | `not found`              | Correct the executable name or `PATH`.                                                                   |
| `spawn`                  | `spawn`                  | Fix permissions, path type, argument size, or the reported operating-system start error.                 |
| `crash`                  | `crash`                  | Run the step directly with the same native input in a controlled test and fix the crash.                 |
| `timeout`                | `timeout`                | Fix the slow step or raise both its internal timeout and the outer host timeout.                         |
| `output_limit`           | `output limit`           | Reduce step stdout below 1 MiB. Put diagnostics on stderr.                                               |
| `output_worker_timeout`  | `output worker timeout`  | Fix descendants that retain inherited stdout or stderr handles.                                          |
| `containment`            | `containment`            | Restore native containment capability; use the failure override only after reviewing reduced guarantees. |
| `unsupported_executable` | `unsupported executable` | Use a real executable image on Windows.                                                                  |
| `malformed`              | `malformed`              | Emit one strict event-valid JSON object, or documented native plain text where permitted.                |
| `unsupported`            | `unsupported`            | Remove fields or field combinations that the selected host event does not accept.                        |
| `oversized`              | `oversized`              | Reduce decision text or native response state; keep mutations small enough to deliver.                   |
| `undeliverable_mutation` | `undeliverable mutation` | Reduce the required replacement. The wrapper cannot discard it and run the original operation.           |

A generic configuration `spawn` response can represent invalid wrapper syntax or invalid native
input. Read developer stderr for the specific diagnostic. Put `--agent` and, for Antigravity,
`--event` before the first syntax error so the wrapper can select a native configuration response;
see the [CLI grammar](reference/cli.md#hook-grammar). Malformed JSON or a payload event the
selected adapter does not serve can instead produce exit 2 with no protocol stdout.

The sequential-hooks Antigravity adapter rejects a successful child output containing only
`systemMessage`. On `PreToolUse`, the default failure policy stops the chain with a deny decision.
On `PostToolUse`, it reports the failure and continues. Do not add a hook that emits such receipts
as a step in an Antigravity chain. See [compatibility](compatibility.md#antigravity-limitations).

Step stderr is relayed with labels and never enters protocol stdout. User-visible messages are
short by design; use the labeled developer output for detail without copying raw host payloads.

See [CLI limits](reference/cli.md#limits), [platform behavior](platforms.md), and the
[host protocol reference](reference/host-protocols.md).
