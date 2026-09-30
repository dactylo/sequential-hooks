# Adding a host

A host is a bundled plugin or an installed adapter package. `sequential-hooks` discovers hosts
through the `sequential_hooks.hosts` entry-point group, selects one by the exact `--agent` name,
and drives it through the public `sequential_hooks.hosts` contract. The built-in Claude Code,
Codex, and Antigravity adapters use this interface too.

## Qualification

A new host must expose a stable local command-hook contract, allow more than one handler for an
event, and lack adequate native sequencing. Confirm those properties from current host
documentation and controlled probes before writing the adapter. A third-party adapter carries its
own evidence and its own compatibility claims; the [compatibility](compatibility.md) page records
versions and known limits for built-in hosts. See [platforms](platforms.md) for process handling.

## The contract

`sequential_hooks.hosts` exports `API_VERSION` and structural types. An adapter package depends on
that subpackage alone, implements the types with its own classes, and never relies on a
sequential-hooks class identity.

- `HostSpec` names the host (`name`, `api_version`, `requires_event`, `events`) and provides three
  operations: `adapter(event, raw_input, payload, diagnostics, allow_on_failure)`, which validates
  native input and returns a `HostAdapter` or raises `HostInputError`;
  `configuration_response(event, reason, diagnostics)`, which returns the host's fail-closed answer
  computed from the host and the event alone; and `doctor_loader()`, which returns a
  `DoctorLoader`. `payload_event(payload)` returns the event a payload carries for hosts that do
  not require `--event`.
- `HostAdapter` exposes `event`, `should_run_steps`, `step_input()`, `consume(step, result)`, and
  `finalize()`. `consume` returns the string `'continue'` or `'stop'`; after `'stop'` the runner
  calls `finalize()` for the terminal response.
- `FinalResponse` has `stdout` bytes and an `exit_code`. `Step`, `StepResult`, and `StepFailure`
  are read-only views the runner constructs; `StepFailure.kind` and `StepResult.containment` are
  strings from the documented `FAILURE_KINDS` and `CONTAINMENT_STATES` sets.
- `DoctorLoader` discovers configuration sources, loads registrations, lists inspection
  limitations, and adds host-specific findings, using the published record types.

## Response composition

An adapter can subclass `Composer` to share response composition. The subclass supplies the
host-specific protocol behavior through these methods:

- `__init__` validates native input and binds the event.
- `classify_unparsable` handles stdout that is not one JSON object, including any permitted plain
  text.
- `parse_structured` accepts or rejects a structured output object.
- `apply_decision` applies the host's decision precedence.
- `build_response` constructs the native final response.
- `failure_texts` and `ordinary_exit_texts` supply report wording.
- `undeliverable_owner` and `terminal_fallback` define size-pressure behavior.

The subclass also sets `pre_tool`, `mutation_field`, `field_limit`, and `blocking_exit`. Set
event-dependent attributes before calling `super().__init__`; fixed attributes can be class
defaults. An adapter may implement `HostAdapter` directly instead. The built-in Antigravity adapter
takes that route.

## Registration and installation

To contribute a bundled host, add one directory under `plugins/`:

```text
plugins/example/
    adapter.toml
    README.md
    _sequential_hooks/
        __init__.py
        _adapter.py
        _doctor.py
    tests/
        test_example_adapter.py
        test_example_doctor.py
```

Declare the entry point in that directory's `adapter.toml`:

```toml
[entry-points."sequential_hooks.hosts"]
example = "plugins.example._sequential_hooks._adapter:ExampleHost"
```

The build collects these registrations and includes every plugin's runtime package. The source
distribution also includes the plugin tests and documentation. No core code or central registration
list needs editing. Leave `plugins/` and `plugins/example/` as namespace directories without
`__init__.py`; the `_sequential_hooks` package keeps this project's files distinct from defsort and
docwrap plugins installed in the same environment. Use unique test module names as shown above.

Plugin code imports the supported `sequential_hooks.hosts` API and its own modules. The API also
provides strict JSON decoding (`load_object`, `load_object_text`) and shared configuration helpers
(`parse_timeout`, `project_layers`, `deduplicate_source_paths`). Agent-specific paths, response
rules, and configuration inspection belong in the plugin. Run `make check` to exercise core and
plugin tests, including distribution packaging.

An externally maintained adapter can instead register its `HostSpec` class in its package metadata:

```text
[project.entry-points."sequential_hooks.hosts"]
example = "example_host.adapter:ExampleHost"
```

The entry point names a zero-argument factory, normally the class; the wrapper calls it.
`api_version` must equal `API_VERSION`, and `name` must equal the entry-point name. From a
sequential-hooks checkout, install the adapter into the wrapper's environment, replacing the
fictional adapter path with the package under test:

```text
uv tool install . --with /path/to/sequential-hooks-host-example
```

Then register the wrapper with the host, naming the adapter explicitly. This fictional `BeforeTool`
event supplies `{"tool":"shell","arguments":{}}` on stdin; both generic children below emit its
invented response protocol:

```text
sequential-hooks --agent example --event BeforeTool python -c 'import json; print(json.dumps({"verdict": "allow", "note": "first child"}))' -- python -c 'import json; print(json.dumps({"note": "second child"}))'
```

## Failure policy and the last resort

A functioning adapter translates child failures into its native response. A configuration error
with a loaded host and a trusted event is answered by that host's `configuration_response`. For
invalid wrapper syntax, only the agent and event options parsed before the first error can supply
that context. When no functioning host exists because the name is unknown or duplicated, the entry
point fails to load or reports another `api_version`; when a payload-event host cannot establish a
served event from the payload, including invalid JSON; or when `adapter()` raises anything other
than `HostInputError` or `configuration_response` itself raises, the wrapper writes nothing to
stdout, one diagnostic line to stderr, and exits 2. Whether that exit blocks is a host property;
document it in the host guide. `doctor --agent NAME` reports an unloadable or mismatched adapter as
`host_unavailable` before any hook runs.

## Evidence and documentation

Provide strict fixtures for realistic input and every accepted output shape, runner tests for
ordering, mutation flow, terminal short-circuiting, diagnostics, and failures, and controlled live
evidence naming host version, operating system, date, configuration source, matcher, event, payload
shape, and result class. Do not call source review or unit tests live evidence. Document the
adapter's installation, events, response rules, failure behavior, trust boundary, and verified host
and platform combinations in its own package. Keep tests and live-host evidence separate so users
can tell which combinations have been exercised.

Read [concepts](concepts.md), [security](security.md), and
[host protocols](reference/host-protocols.md) before proposing a host.
