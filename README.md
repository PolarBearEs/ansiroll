# Ansiroll

Compile an Ansible playbook into a **single self-contained Python file** that runs
the whole play on a host using only Python (no Ansible required on the target), and
run plays via an **`ansiroll` strategy plugin** that ships that one file to each
host and reports normally (`ok`/`changed`/`failed`/`skipped`/`rescued`/`ignored`
and a matching `PLAY RECAP`).

The name combines the project's Ansible roots with what it does: it rolls a
playbook into one self-contained artifact and rolls that artifact out to hosts.

## How it works

1. **Compile.** `ansiroll` reuses ansible-core's own parser (so static
   imports/roles are already inlined), lowers each play into a small data model,
   and builds a shared import bundle from every referenced AnsiballZ payload.
   Modules and `module_utils` are stored once by import path; baked files and
   templates are content-addressed in a separate data bundle. Both bundles are
   Base64-encoded inside the generated Python source, then decoded and imported
   in memory;
   Ansiroll does not write or extract them to temporary files on the target.
   The import bundle includes private pure-Python Jinja2 and YAML runtimes, so
   filters such as `from_yaml`, `to_yaml`, and `to_nice_yaml` do not depend on
   target packages.
2. **Run.** A tiny interpreter (`ansiroll/runtime.py`, inlined into the output)
   templates args/conditions, evaluates `when`, expands `loop`, runs the embedded
   modules as subprocesses, tracks `register`/facts, runs handlers, and emits one
   framed JSON result per task. Baked local sources for the official `unarchive`
   module are passed through the worker protocol into an anonymous Linux memfd;
   they are never staged in `/tmp` or another filesystem.
3. **Report.** The strategy plugin parses those frames and replays them through the
   normal callback machinery, so output is indistinguishable from a normal run.

## Install / use (dev)

```bash
export PYTHONPATH="$PWD"
export ANSIBLE_COLLECTIONS_PATH="$PWD/collections"
```

### Compile a playbook to one file

```bash
python3 -m ansiroll.cli compile playbook.yml -o compiled.py -m compiled.json
# copy it anywhere with Python and run it directly (no Ansible needed):
ANSIROLL_HOST=myhost python3 compiled.py
```

The Python output and manifest need distinct destinations. If the filesystem
only reveals an alias when creating a file, the compiler rejects it before
writing data and may leave an empty output file.

When overwriting files on a filesystem that requires truncation at open time,
the compiler uses Linux `/proc/self/fd` to reopen the validated file. If the
initial create-open fails, this retry requires a file owned by the compiler's
effective user. If the safe reopen is unavailable, compile to local staging
files and copy the outputs to the intended destinations.

Use `-o -` to stream the generated Python directly to the interpreter. In Bash,
`pipefail` makes the pipeline report a compiler failure even if Python exits
successfully.

```bash
set -o pipefail
python3 -m ansiroll.cli compile playbook.yml -o - | python3 -
```

Keep stdout and stderr separate. Send stdout to a pipe or file because a terminal
can transform the generated source.

Configure any Python startup hooks, such as `sitecustomize`, to send their
messages to stderr so they cannot add text to the pipeline before Ansiroll runs.
When Ansiroll and Ansible are installed in the interpreter's environment, use
`python3 -I -m ansiroll.cli` to ignore startup hooks from `PYTHONPATH` and the
user site directory.

Compile-time extra vars can optionally remove branches that are provably
unreachable. Unknown conditions are retained, and the resulting artifact rejects
conflicting runtime values for specialized variables:

```bash
python3 -m ansiroll.cli compile playbook.yml -o update.py \
  --tree-shake --freeze-var run_mode -e @site-vars.yml
```

Without `--tree-shake`, compilation remains generic. `-e` accepts Ansible-style
`key=value`, JSON/YAML mappings, and `@file`; later arguments win. Use repeatable
`--freeze-var NAME` to make selected play/group variables immutable, or
`--freeze-playbook-vars` to freeze every safely resolvable top-level play variable.
Frozen and `-e` values are recorded as fixed build-time values, and conflicting
runtime overrides are rejected. Values passed with `-e` are embedded in the
artifact, including values used only to resolve a controller-side lookup.
When a standalone play's `gather_timeout` uses a controller lookup, supply the
lookup inputs and any vars-file selectors it depends on at compile time.
Environment values referenced only by the contents of runtime-selected
`vars_files` are omitted from the artifact. For runtime-selected data, use a
play variable and supply its value at launch through `ANSIROLL_EXTRA_VARS` or
the JSON file named by `ANSIROLL_VARS_FILE`.

### Run a playbook locally (compile + execute)

```bash
python3 -m ansiroll.cli run playbook.yml
```

### Run via the strategy plugin

```bash
ANSIBLE_STRATEGY=ansiroll.core.ansiroll ansible-playbook playbook.yml
# or set `strategy: ansiroll.core.ansiroll` per-play, or in ansible.cfg.
```

### Optional accelerated modules

Official embedded Ansible modules remain the default unless the compiler can
prove that every surviving call of an action uses a replacement-safe
accelerator. To opt into the other stdlib-only accelerated implementations,
select them by short name or use `all`:

```bash
ANSIROLL_ACCELERATED_MODULES=apt,command,shell,file,stat,systemd python3 compiled.py
ANSIROLL_ACCELERATED_MODULES=all \
  ANSIBLE_STRATEGY=ansiroll.core.ansiroll ansible-playbook playbook.yml
```

The compiler marks literal supported calls once. Those calls have no repeated
capability check at runtime; templated arguments are checked after rendering.
Unsupported argument combinations, check-mode command calls, and unselected
modules use the official bundled implementation. The accelerated modules live
under `ansiroll/accelerated_modules/` so each implementation and its parity
tests can evolve independently.

Replacement-safe accelerators are deliberately a smaller set. They must match
the official result schema in differential tests, support check mode, have no
host-dependent fallback, and be used only by statically proven calls. The
compiler marks those calls as required accelerators and omits their official
payload; `accelerated_only_modules` in the manifest records the decision.
Currently only `stat` meets this stricter contract. With tree shaking, fixed
build-time variables may prove a templated call without rewriting its runtime
arguments.

The `systemd` accelerator is deliberately conservative: it handles the common
system-scope `state`, `enabled`, and `daemon_reload` operations. Check mode,
masking, user/global scopes, reload state, ambiguous reload unit names, missing
units, and other options fall back to the official Ansible module.

The `apt` accelerator handles explicit package lists with `state: absent`. It
uses `dpkg-query` for a fast no-op check and invokes the real `apt-get remove`
when a package or purged configuration remains. Installations, upgrades,
wildcards, cache updates, unusual dpkg states, and other options fall back to
the official module before making changes.

Fact gathering has a separate opt-in because Ansible's `setup` module exposes a
much larger surface than the accelerated task modules:

```bash
ANSIROLL_FAST_FACTS=1 python3 compiled.py
```

The fast collector provides distribution, kernel, mount-space, and date/time
facts for latency-sensitive Debian-family systems. If those values cannot be
established, it falls back to the bundled official `setup` module. Leave this
unset for playbooks that consume other setup facts or request setup
subsets/filters. Its
date/time schema follows the ansible-core version used to compile the artifact,
including the additional fields introduced after ansible-core 2.10.

Set `ANSIROLL_PROFILE=1` on a direct compiled-file run to print aggregate JSON
summaries for module calls and engine phases. Engine timings include bootstrap,
worker authentication/spawn/bundle transfer, play execution, cleanup, and time
outside profiled modules. The engine summary also includes `slow_tasks` and
`slow_engine_tasks`, with module time separated from Ansiroll-owned time for
each task, plus `worker_identities` showing which privilege identities actually
started. A `become_user` that resolves to the agent's current UID shares the
normal worker instead of launching a redundant sudo worker. This is intended for
deciding whether another runtime optimization or accelerator can materially
improve a real playbook.

## Status & supported features (v1)

Working and verified at parity with `ansible-playbook` (linear):

- tasks, `pre_tasks`, `post_tasks`, roles (incl. `defaults`/`vars` precedence)
- `when`, `loop`, `register`, `changed_when`, `failed_when`, `ignore_errors`
- check mode (CLI, play, and task overrides), `run_once`, and sudo `become_user`
- `block`/`rescue`/`always`, handlers + `notify` + `flush_handlers`
- runtime specials: `debug`, `set_fact`, `fail`, `assert`
- any module that builds via AnsiballZ (`command`, `shell`, `copy`, `file`, ...)
- gather_facts (runs the embedded `setup` module)

Reported and tallied like Ansible: `ok/changed/failed/skipped/rescued/ignored`.

## Execution model (persistent channel)

The strategy ships the compiled play as a long-lived **agent** and drives it over a
single persistent bidirectional channel per host:

- the agent runs the **whole play in one process** — the module worker stays warm
  (module_utils compiled once), so there's no per-task or per-segment cold start;
- each task result is **streamed live** back to the controller as it completes
  (normal `ok:`/`changed:` output, not one dump at the end);
- `delegate_to` / controller-coupled tasks are handled **inline** — the agent asks
  the controller over the same channel and continues (no re-ships, no segmentation,
  and `register`/fact state simply stays live in the agent).

Transport reuses Ansible's connection plugin: `local` runs python directly; `ssh`
reuses the ssh plugin's own command (keys/args/ControlPersist/sshpass). Ansiroll
does not provide transport-specific command runners.

## Native-only parity and provenance

Set `ANSIROLL_NATIVE_ONLY=1` (or the `ansiroll_native_only: true` variable)
to reject compatibility fallback. A play fails before execution if it requires
core `linear`, controller execution, or delegation fallback; the runtime repeats
the check before any dynamically reached controller fallback.

Set `ANSIROLL_PROVENANCE=1` (or `ansiroll_provenance: true`) to print
machine-readable `ANSIROLL_PROVENANCE` JSON lines. Each play/task is classified
as `native-agent`, `real-ansiballz`, `controller-fallback`, or
`linear-fallback`. Provenance is transport metadata and is not inserted into
registered task results.

## Delegation (`delegate_to`)

`delegate_to` tasks are executed on the correct host inline over the channel; the
agent keeps running the rest of the play with no interruption to its warm worker.

- `delegate_to: localhost` (and `127.0.0.1`/`::1`) runs on the **controller** — correct.
- `add_host`, `group_by`, and `include_vars` are serviced by the controller.
- `delegate_to: <other host>` and other controller-only action plugins (`fetch`,
  `reboot`, …) are **not supported yet** and fail with a clear message.
- Set **`ansiroll_delegate_on_target: true`** (play/host/extra var) to run every
  task on the target instead (the agent skips the controller callback) — correct
  only when delegation doesn't actually need the controller (e.g. a Slack call that
  works from the target too).

## Known limitations (v1)

- **Templating fidelity** uses the bundled Python-3.5-compatible Jinja2 runtime.
  Custom Ansible filters/tests/lookups are not all available yet — this is the
  main place behavior can diverge from real Ansible.
- **Controller-coupled tasks** such as `fetch`, cross-host `delegate_to`, and
  dynamically named `include_*` remain unsupported. Static includes are expanded
  at compile time; `add_host`, `group_by`, and `include_vars` run on the controller.
- Hosts execute the play one at a time rather than in task lockstep. Common
  `run_once` results/facts are shared across the current host batch, but dynamic
  cross-host state can still differ from `linear`. Multi-host
  `any_errors_fatal` and `max_fail_percentage` are rejected before any host starts.
  The full ansible-core 2.21 `ignore_errors` integration target is a known
  reproducer because it asserts one host's registered result from another host
  before host-sequential execution reaches it; it remains available with
  `run_ansible_test_suite.py 2.21 --target ignore_errors` but is not counted as a
  passing smoke target.
- Connection transport is covered end-to-end with both `local` and a real OpenSSH
  server. Other connection plugins are not yet supported.
- Module embedding targets the AnsiballZ format of the ansible-core used **at
  compile time**; the compiled file itself needs only Python on the target.

## Tests

```bash
pip install -e ".[test]"
pytest                      # full suite
pytest --cov=ansiroll --cov-report=term-missing --cov-report=html
pytest tests/test_filters.py tests/test_runtime.py   # unit only (no Ansible needed)
python tests/run_version_matrix.py  # matching upstream suites for 2.19/2.20/2.21
python tests/run_version_matrix.py 2.12 2.14 2.16 2.18  # legacy smoke parity
python tests/run_version_matrix.py 2.10 --skip-repo-tests  # best-effort smoke
python tests/run_ansible_test_suite.py 2.20 --profile full  # 10 complete upstream targets
python tests/run_ansible_test_suite.py 2.20 --profile native --native-only --provenance
python tests/run_ansible_test_suite.py 2.12 --target failed_when  # one target
```

- `test_filters.py` / `test_runtime.py` — pure unit tests of the runtime
  interpreter, vendored Jinja2 filters and tests (no Ansible required).
- Coverage is branch-aware and enforces a 50% project floor. The coverage command
  above prints missing lines and writes a browsable report to `htmlcov/index.html`.
- `test_integration.py` — module embedding, compilation, legacy-`include` rewrite,
  implicit fact gathering (auto-skips without ansible-core).
- `test_parity.py` — compiles + runs the fixtures three ways (real Ansible, the
  strategy, and standalone) and asserts the PLAY RECAPs match.

CI (`.github/workflows/ci.yml`) runs the repository suite across **ansible-core
2.12 → 2.21** (plus best-effort `ansible-base 2.10.17`) and compares curated
upstream integration targets from each matching release tag against its own
`linear` strategy. The 2.20 reference lane runs the full curated suite; adjacent
maintained releases use the portable profile and older releases use smoke parity.
An additional `ansible-test` matrix runs complete upstream targets—including
their `runme.sh`, setup dependencies and harness metadata—twice in fresh Docker
controllers: first with `linear`, then with an injected disposable copy of
Ansiroll. It compares the command result, every `PLAY RECAP`, and task/status
trace. The required 2.20 lane runs ten validated complete targets (`tags`,
`register`, `changed_when`, `failed_when`, `ignore_errors`, `templating`,
`conditionals`, `set_fact`, `blocks`, and `strategy_linear`); other release
lanes retain version-appropriate smoke targets. Its isolated home, source
worktree, and generated SSH material live under
`/tmp/ansiroll-version-matrix`; it does not run integration playbooks directly
on the host.

A separate required 2.20 native-only lane currently runs the complete
`conditionals` and `set_fact` targets. `ANSIROLL_NATIVE_ONLY=1` rejects a play
before execution if it would use core `linear`, controller execution, or
delegation fallback. `ANSIROLL_PROVENANCE=1` emits one JSON record per play and
task with `native-agent`, `real-ansiballz`, `controller-fallback`, or
`linear-fallback`; CI asserts the native-only lane contains no fallback records.

See `PLAN.md` for the full design and rationale.

## License and generated artifacts

Ansiroll's original source code is available under the [MIT License](LICENSE).
The bundled Jinja2 and MarkupSafe sources retain their respective BSD licenses,
and the bundled PyYAML sources retain their MIT license. Their license texts are
included in the corresponding archives under `ansiroll/vendor/`.

Compiled agents may contain modules and supporting code sourced from the
installed Ansible distribution. Those embedded components remain subject to
their upstream licenses, including the GNU GPLv3 where applicable. If you
redistribute a generated agent, you are responsible for complying with the
licenses of the components embedded in it.
