# Ansiroll — Architecture and Roadmap

## Purpose

Ansiroll has two related deliverables:

1. A compiler that turns an Ansible playbook into one self-contained Python
   program. The generated program runs the complete play locally and requires
   only Python on the target host.
2. An Ansible strategy plugin that compiles a play in-process, starts the
   generated program once per host, streams normal task results back to the
   controller, and handles controller-coupled work over the same persistent
   channel.

The project supports ansible-base/ansible-core 2.10 through 2.21 at compile and
strategy runtime. The oldest 2.10 lane is best-effort because of its packaging
and interpreter constraints.

This document describes the implemented architecture and the remaining roadmap.
The user-facing feature list and commands live in `README.md`.

## Design constraints

- The compiled artifact is a Python file, not flattened YAML.
- A standalone artifact must not require Ansible, Jinja2, or extracted support
  files on the target.
- Task state remains inside one long-lived runtime: variables, facts, registered
  results, notified handlers, block state, and loop state are not reconstructed
  between tasks.
- Ansible's parser and AnsiballZ builder remain controller-side compile-time
  dependencies.
- Controller-coupled operations must either be baked into the artifact or
  serviced explicitly by the strategy. Unsupported operations must fail clearly,
  never silently run with different semantics.
- Compatibility with private ansible-core APIs is isolated where practical and
  continuously tested against each supported release.

## Repository layout

```text
Ansiroll/
├── pyproject.toml
├── ansiroll/
│   ├── cli.py                  # `ansiroll compile|run`
│   ├── loader.py               # Ansible parsing and task-model conversion
│   ├── compiler.py             # specialization, bundles, source and manifest
│   ├── embed.py                # AnsiballZ module embedding
│   ├── compat.py               # ansible-core compatibility helpers
│   ├── runtime.py              # interpreter inlined into generated artifacts
│   ├── worker.py               # persistent module worker embedded as source
│   ├── accelerated_modules/    # optional stdlib module implementations
│   └── vendor/
│       └── jinja2-py35.zip     # target-side Jinja2/MarkupSafe bundle
├── collections/ansible_collections/ansiroll/core/
│   ├── galaxy.yml
│   └── plugins/strategy/ansiroll.py
└── tests/
    ├── fixtures/
    ├── test_*.py
    ├── run_version_matrix.py
    └── run_ansible_test_suite.py
```

## Compiler

`ansiroll compile playbook.yml -o compiled.py [-m manifest.json]`

### Parse and lower

`ansiroll.loader` uses ansible-core's `DataLoader`, `VariableManager`, and
playbook objects. Ansible expands static imports and roles; the loader then
converts plays, blocks, tasks, handlers, and supported includes into a compact,
JSON-serializable task model.

The lowered model records the data needed by the target runtime, including:

- task arguments, conditions, loops, registers, tags, and task variables;
- block/rescue/always structure;
- handlers and notifications;
- check, diff, become, delegation, and `run_once` metadata;
- baked controller-side sources for supported actions;
- accelerator capability decisions; and
- stable runtime task identifiers for callback reconstruction.

Dynamic or controller-specific behavior is classified during lowering. Static
`include_tasks` and `include_role` targets are expanded when resolvable.

### Specialize

Normal compilation keeps runtime behavior generic. Optional tree shaking uses
fixed extra variables and explicitly frozen play/group variables to:

- remove provably unreachable nodes;
- simplify conditions;
- prove selected accelerated-module calls safe; and
- omit module payloads that no surviving task can use.

The manifest records specialization inputs and decisions without recording the
fixed values themselves. The generated artifact rejects conflicting runtime
overrides for specialized variables.

### Embed modules and assets

`ansiroll.embed.ModuleEmbedder` resolves each distinct module and asks the
installed ansible-core to build its AnsiballZ wrapper. The compiler extracts the
module archive and import name, then deduplicates shared modules and
`module_utils` by import path.

The compiler creates two deterministic ZIP bundles:

1. An import bundle containing module code, shared `module_utils`, the vendored
   Jinja2 runtime, and the accelerator implementations.
2. A data bundle containing the lowered plays, module metadata, baked group
   variables, specialization data, and content-addressed task assets.

Both bundles are Base64-encoded into the generated source and opened in memory.
No support archive is extracted on the target.

### Outputs

The compiler returns:

- a standalone Python source artifact; and
- a versioned JSON manifest describing plays, embedded and unsupported modules,
  controller fallbacks, accelerator-only modules, bundle statistics, and
  specialization decisions.

The manifest is diagnostic build metadata. The current strategy compiles its own
in-memory artifact and does not use the sidecar to segment execution.

## Target runtime

`ansiroll.runtime` is inlined into every generated artifact. It:

- initializes play, host, group, extra, fact, and registered variables;
- renders arguments and conditions with the bundled template environment;
- evaluates `when`, loops, changed/failed conditions, and block control flow;
- runs runtime-special actions such as `debug`, `set_fact`, `fail`, `assert`,
  and `meta`;
- manages handlers, notifications, check mode, and facts;
- delegates module execution to a persistent worker; and
- emits framed task results and optional profiling summaries.

The worker is started once per effective privilege identity. It receives the
shared module bundle once, keeps decoded and compiled imports warm, and forks a
child for each module invocation so module globals and `sys.exit` calls remain
isolated. Linux `memfd` is used for supported baked-file operations that require
an anonymous file descriptor.

Direct runs render Ansible-like human-readable output. Strategy/agent mode uses
length-prefixed JSON messages on a persistent bidirectional channel.

## Strategy plugin

The `ansiroll.core.ansiroll` strategy:

1. Converts and compiles the current play in the controller process.
2. Starts one agent for each host using the active Ansible connection plugin.
   Local and OpenSSH connections are supported.
3. Sends initial host variables, extra variables, execution flags, and the module
   bundle over the channel.
4. Receives each task result as it completes and reconstructs the appropriate
   Ansible task result and callback.
5. Services supported controller requests inline, returning the result to the
   still-running agent.
6. Updates Ansible statistics and drains the play iterator after the host's agent
   completes.

The persistent channel replaces the earlier segmented/re-ship design. Registered
values, facts, and handler state stay live in the agent while a controller task
is serviced.

### Controller-coupled work

The strategy currently services:

- `delegate_to: localhost` and loopback aliases;
- `add_host`;
- `group_by`; and
- `include_vars`;
- delegated `set_fact` and local `template`;
- `synchronize` through Ansible's native task executor; and
- runtime-created controller source files requested by supported file actions.

Cross-host delegation and other controller-only action plugins such as `fetch`
and `reboot` are not yet supported. A play may opt into
`ansiroll_delegate_on_target` only when running delegated work on the target is
semantically correct.

## Execution semantics and known limits

- Hosts run their whole play sequentially rather than advancing in task
  lockstep. Common `run_once` results and facts are shared across the current
  host batch, but dynamic cross-host state can differ from Ansible's `linear`
  strategy.
- Multi-host `any_errors_fatal` and `max_fail_percentage` are rejected before
  starting hosts because they require lockstep decisions.
- Template fidelity is intentionally a supported subset. The runtime implements
  commonly used Ansible filters, tests, and lookups and can load baked custom
  Jinja plugins, but it is not the complete controller plugin ecosystem.
- Static includes are expanded when resolvable. Dynamically named includes remain
  limited.
- Connection handling is verified for `local` and OpenSSH. Other connection
  plugins are not yet supported.
- The generated artifact is tied to the AnsiballZ/module format of the
  ansible-core version that compiled it, but requires only Python when executed.

## Optional acceleration

The official embedded Ansible module is the default implementation.
`ANSIROLL_ACCELERATED_MODULES` opts into smaller stdlib implementations for
calls whose arguments are supported.

An official payload may be omitted only when an accelerator is
replacement-safe:

- supported calls are proven statically;
- check-mode and result-schema behavior match the official module;
- differential tests cover the supported surface; and
- execution does not depend on a host-specific fallback.

Calls that are unsupported, dynamic, or not selected use the official bundled
module. Fast fact gathering is a separate opt-in because `setup` exposes a much
larger fact surface.

## Verification

The repository uses three levels of verification:

1. Unit tests for templating, filters, runtime state, task execution, workers,
   accelerators, the compiler, and helper scripts.
2. Repository integration and parity tests comparing standalone execution, the
   strategy plugin, and Ansible's `linear` strategy.
3. Version-matched upstream tests across ansible-base/ansible-core 2.10 through
   2.21, including selected upstream integration targets and complete
   `ansible-test` targets in isolated Docker controllers.

Parity comparisons cover command status, task/status traces, and every
`PLAY RECAP`. The maintained reference release runs the broadest target set;
adjacent releases use portable targets and older releases use stable smoke
targets.

## Roadmap

### Correctness

- Expand filter, test, lookup, and custom-plugin parity based on real playbooks.
- Support more dynamic include cases without changing Ansible variable
  precedence.
- Add cross-host delegation and additional controller-only action protocols.
- Improve host-batch semantics where they can be made equivalent without losing
  the persistent-agent execution model.
- Continue differential testing before promoting accelerators to
  replacement-safe status.

### Maintainability

- Split the large runtime, loader, and strategy sources into focused source
  modules while preserving single-file generation.
- Narrow broad compatibility exception handling and add explicit diagnostics
  where a fallback path is selected.
- Expand linting incrementally beyond undefined and unused-name checks.
- Keep this document, the README, and the implementation synchronized in CI.

### Distribution and security

- Define and test the installation contract between the Python package and
  Ansible collection.
- Build and install both release artifacts in clean CI environments.
- Complete license, repository, and package metadata.
- Treat generated artifacts as potentially secret-bearing: create them with
  restrictive permissions and document their storage and transfer requirements.
