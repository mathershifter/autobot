# Autobot test plan

This plan comes from the autobot-reviewer brief of 2026-09-28 (findings #1 to #17). SPEC.md is the source of truth. README.md is used only where SPEC is silent and the README documents the behavior on purpose.

The plan only covers tests. It changes no product code. It adds to the existing suite (55 tests in 5 files) and does not duplicate it. Where an existing test already covers a behavior, it is listed in the coverage matrix and not planned again.

## Status legend

| Status | Meaning |
|--------|---------|
| `pass` | Encodes SPEC and should pass on current `main`. |
| `xfail #N` | Encodes SPEC; current code disagrees. Mark `@pytest.mark.xfail(strict=True, reason="finding #N")`. |
| `todo #N` | Was blocked on spec decision #N. The decision has landed in SPEC.md (see [Spec decisions](#spec-decisions)), and the Assertion column gives the target. Not written yet; expected to be `pass`. |
| `slow` | Needs the fixed 5 s idle poll in `get_prompt` (can't be shortened without a product change). Mark `@pytest.mark.slow`. |

Finding numbers #1 to #17 come from the reviewer brief. #18 and #19 are new; I found them while writing this plan (see [New findings](#new-findings-from-planning)). #20 came up while fixing #19.

## Summary

| Priority | Area | pass | xfail | todo | total | slow |
|----------|------|-----:|------:|---------:|------:|-----:|
| P1 | `cmd` success semantics, register, ignore_error | 22 | 0 | 0 | 22 | 1 |
| P2 | `cmd` forms, embedded scripts | 20 | 0 | 0 | 20 | 0 |
| P3 | Common step properties, templating context | 18 | 0 | 0 | 18 | 0 |
| P4 | `get_prompt`, prompts, credential cycling | 28 | 0 | 0 | 28 | 3 |
| P5 | attach / block lifecycles, env | 42 | 0 | 0 | 42 | 0 |
| P6 | model / schema / example / CLI parity | 54 | 0 | 0 | 54 | 0 |
| P7 | registry and plugins | 13 | 0 | 0 | 13 | 0 |
| P8 | Low priority: types, strip_echo, simple steps, log output | 16 | 0 | 0 | 16 | 0 |
| **Total** | | **213** | **0** | **0** | **213** | **4** |

The P6 row was 40 while its section already listed 45 (P6-41..45 weren't added here); it now counts P6-01..51.

A parametrized test counts as one test.

## Fixtures (F1 to F7)

F3 was retired: nothing used it, and it has been removed from `conftest.py` (see [F3](#f3-retired)).

All shared fixtures go in `tests/conftest.py`. Device fakes go in `tests/fakes/`. The four copies of a `run()` helper in `test_output_capture.py`, `test_embedded_script.py`, `test_plugin_common_props.py` and `test_lifecycle.py` get folded into F1 in the same PR that adds the fixtures. That refactor must not change what those tests assert.

Register the marker in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = ["slow: needs the 5s get_prompt idle poll; deselect with -m 'not slow'"]
```

### F1: real local shell (`tests/conftest.py`)

- Constants: `BASH = "bash --norc --noprofile -i"`, `SHELL_ENV = {"TERM": "dumb", "PS1": "PROMPT$ ", "PATH": os.environ["PATH"]}`, `SHELL_PROMPT = {"name": "sh", "expect": [r"PROMPT\$ "], "return": True}`.
- `make_config(script, *, prompts=[SHELL_PROMPT], errors=None, vars=None, env=None, fn=None, spawn=BASH, attach_env=SHELL_ENV, attach_script=None, breakout=None, prepare=None, timeout=5) -> Config`. It also sets `timeout: "5s"` on every top-level step that allows `timeout` and doesn't set one.
  - There is no default `attach_script`. Until #3 was fixed it defaulted to a `[{"return": 1}]` kick, because the initial attach wait swallowed bash's first prompt. Now that prompt stays pending, and a kick would produce a second prompt that shifts every command's captured output by one.
- `run_script(script, **kw) -> Runner`: builds the config, calls `Runner(cfg, {}).run()`, and returns the runner. Registered values are in `runner.config.vars`.
- `attached_runner` fixture (a factory): builds a `Runner`, calls `session.attach(BASH, env=SHELL_ENV, timeout=5)` and returns the runner with bash's first prompt pending, so tests can call `run_steps()` directly. Teardown calls `session.detach()`.
- `shell_session` fixture: a raw `Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])` attached to bash. Teardown detaches it.
- `steps(list_of_dicts) -> list[Step]`: validates steps through `Config` (replaces `test_lifecycle._steps`).
- `children` fixture: moved from `test_lifecycle.py` without changes. It records every child passed to `Session.detach`.

### F2: scriptable fake device (`tests/fakes/device.py`)

This is a standalone `python3` program used as `attach.spawn` (`f"{sys.executable} {DEVICE} ..."`). The `fake_device` fixture builds the command line and returns `(spawn_cmd, log_path)`. `log_path` is in `tmp_path`.

Options:

| Option | Behavior |
|--------|----------|
| (no option) | the banner `Welcome` is printed first |
| `--wait-enter` | after the banner, block on one input line before prompting. Tests stay deterministic: the device only continues when Autobot sends something. |
| `--same-chunk` | write the banner and the first prompt in one `os.write` (reproduces #3) |
| `--order login,password` / `password,login` / `password` / `none` | prompt sequence per attempt; `login:` / `Password:` text. `none` skips authentication (used for shell-prompt-only tests). |
| `--accept USER:PASS` (repeatable) | credentials that succeed. A rejected attempt prints `Login incorrect` and restarts the sequence. |
| `--post-auth-delay SECS` | sleep before printing the shell prompt after auth (for "no solicit after handler") |
| `--repeat N` | after auth, print `PROMPT$ `, read one line, and run the whole login sequence again (N times) |
| `--silent` | turn off tty echo, print the banner and never print anything else (read and log input as `SILENT=`) |
| `--exit-after-banner` | exit after the banner (after the enter, if `--wait-enter`) |
| `--ask TEXT` (repeatable) | after the enter (and `--rawdump`), print `TEXT ` and log the reply as `ASK=<reply>`, once per option, in order, before the login prompts. A question for simple prompts. |
| `--rawdump N` | put the tty in raw mode, print `RAW> `, read N bytes, log them as hex, restore, print `RAW=<hex>`, and continue. The two markers let a script synchronize with `after`. |
| `--then shell` | after auth, `os.execvpe("bash", [..."--norc","--noprofile","-i"], {PS1: "PROMPT$ ", TERM: "dumb", PATH})` (default); `--then prompt` prints `PROMPT$ ` and loops on `readline` echoing nothing |

Every line the device reads is appended to `log_path` as `<PROMPT>=<value>` (e.g. `LOGIN=admin`, `PASSWORD=secret`, `ENTER=`, `RAW=1d`). Tests assert on the log file, not on parsed pty output. Before each prompt, the device flushes stdout.

### F3 (retired)

F3 was planned as in-process `FakeSession`/`FakeCtx` doubles implementing `RunnerContext`, for a `get_prompt` that raises `ValueError`/`EOFError` at an exact point in the P1-20 (#10) tests. Those tests use the real shell for every case, so the doubles had no users and were removed. The ID is not reused.

### F4: send recorder (`tests/conftest.py`)

The `sent` fixture monkeypatches `pexpect.spawn.sendline` and `pexpect.spawn.sendcontrol`. It appends `("line", s)` / `("ctrl", c)` to a list and then calls the original. It sees commands, prompt responses, solicit newlines and `check_rc` probes, because they all go through the child. Helpers:
- `sent.lines()`: all sent lines.
- `sent.commands()`: sent lines minus `""` and minus `echo __AUTOBOT_RC=$?`.
- `sent.clear()`: called after setup so assertions only see the step under test.

A second fixture, `spawned`, monkeypatches `pexpect.spawn.__init__` to record `(command, kwargs)` and then calls the original. It's used for env and timeout assertions. With `spawned.stop = True`, it raises a `SpawnRecorded` sentinel after recording instead of spawning, so tests can check spawn arguments without a real child.

### F5: timeline recorder (`tests/conftest.py`)

This generalizes `sleeps` from `tests/test_plugin_common_props.py`. It monkeypatches `Session.sleep` to a no-op that records `("sleep", seconds)`, and wraps `Session.expect`, `Session.get_prompt`, `Session.sendline`, `Session.reset_handlers`, `Session.restore_handlers` and `Session.attach` to append `(name, args)` to one shared `timeline` list before they delegate to the real method. Use it for ordering assertions. `sleeps` stays available as `timeline.sleeps()`.

### F6: registry isolation (`tests/conftest.py`)

- `isolated_registry` fixture: creates a fresh `StepRegistry` and calls `register_builtins` on it. It monkeypatches `autobot.registry.registry` and `autobot.runner.registry`. `autobot.models` imports the registry lazily, so the module-attribute patch covers it. It also wraps `importlib.metadata.entry_points` with a call counter (`reg.discover_calls`).
- `plugin_dist(tmp_path, key, source)` helper: writes `<mod>.py` plus `<mod>-0.1.dist-info/{METADATA,entry_points.txt}` with an `[autobot.steps]` entry. Generalizes `test_plugins.plugin_path`. Use it with `monkeypatch.syspath_prepend`, or with `PYTHONPATH` for CLI subprocesses.
- `register_plugin(executor)` fixture: registers the executor on the global registry and removes it from `_executors` and `_model_keys` in teardown. Generalizes `test_plugin_common_props.probe`.
- `ProbeExecutor`: records `(step, timeout, [h.name for h in ctx.session.save_handlers()], id(handlers))` on each `execute`. It's used to look at session state from inside a script.
- Rule: never call `discover()` on the global registry while a fake dist-info is on `sys.path`.

### F7: JSON schema validator (`tests/conftest.py`)

- Add `jsonschema` to `[dependency-groups] dev` in `pyproject.toml` and run `uv lock`. Add `pytest-cov` the same way, for coverage reports.
- `schema_validator` fixture: `pytest.importorskip("jsonschema", reason="jsonschema not installed; parity tests skipped")`, loads `schemas/autobot.2026-10.json`, and returns `Draft202012Validator(schema)`.
- `both_validate(doc) -> tuple[bool, bool]`: returns `(model_ok, schema_ok)`, so parity tests can assert `== (True, True)`, `== (False, False)`, or pin a specific divergence.

## Coverage matrix (SPEC section → existing → planned)

| SPEC section | Existing tests | Gaps closed by |
|--------------|----------------|----------------|
| Architecture / validation (SPEC.md:5-18) | `test_plugins::test_typo_step_key_*` | P6 |
| Top-level fields, `autobot` version, `env` (SPEC.md:20-31) | none | P6-05, P6-15, P5-19..22, P5-27..31, P6-31 |
| `prompts`, `send` forms, `sendEach`, Response selection | none | P4-12..15, P4-17, P4-20..31, P5-25/26, P6-07, P6-38..40, P6-46..59 |
| `fn` / `call` (SPEC.md:55-66, 180-184) | none | P3-15, P8-12, P7-08 |
| `attach` fields and lifecycle (SPEC.md:68-85) | `test_lifecycle::test_attach_*` (breakout failure, error preserved) | P5-01..09, P5-17..24, P5-32..34, P5-42..45, P8-14 |
| `cmd` basic / multiline / list (SPEC.md:89-99) | `test_output_capture::test_list_registers_all_lines`, `test_multiline_string_registers_all_lines` | P2-01..06 |
| Per-line `errors`, success semantics (SPEC.md:101-107) | `test_output_capture::test_errors_*`, `test_list_error_on_middle_line_*`, `test_assert_*` | P1-01..11 |
| Captured output (SPEC.md:109-116) | `test_output_capture` (echo, long echo, no trailing newline, echo off, strip_echo table) | P1-17/18/19, P8-05 |
| `ignore_error` (SPEC.md:118-123) | `test_ignored_error_registers_output`, `test_ignored_rc_failure_registers_output`, embedded `rc_ignored` | P1-15, P1-20 |
| `register` (SPEC.md:125-138) | `test_output_capture` register tests | P1-12..16 |
| Embedded scripts (SPEC.md:140-172) | `test_embedded_script` (round-trip, removed on success/failure/upload failure, cleanup errors, bounded cleanup with `^C` ignored) | P2-07..20 |
| `sleep` (SPEC.md:174-178) | none | P3-11, P8-11 |
| `block` (SPEC.md:186-250) | `test_lifecycle::test_block_*` (breakout timeout, failing enter, error preserved, template error) | P5-10..16, P5-35..41, P4-32..34 (prompt state across a swap) |
| `line` / `return` / `control` (SPEC.md:252-270) | none | P8-06..09 |
| Common step properties, order (SPEC.md:272-288) | `test_plugin_common_props` (plugin only) | P3-01..16 |
| `when` (SPEC.md:290-305) | `test_plugin_when` (2 values) | P3-02..06 |
| Duration format (SPEC.md:307-313) | none | P6-09, P8-01 |
| Jinja2 templating, filters, `range` (SPEC.md:315-334) | none | P3-06..09, P4-18, P8-09/10, P5-05 |
| `get_prompt` (SPEC.md:336-346) | `test_lifecycle::test_expect_timeout_*`, `test_sleep_and_check_rc_eof_*` (session exceptions only) | P4-01..20 |
| CLI (SPEC.md:348-355) | `test_plugins::test_plugin_step_runs_through_cli`, `test_typo_step_key_is_clean_cli_error` | P6-21..37, P7-07, P7-09..11 |
| Plugins (SPEC.md:286) | `test_plugins` (discovery lazy/idempotent), `test_plugin_common_props` | P7-01..12 |
| Examples | none | P6-19/20 |

## P1: `cmd` success semantics

File: `tests/test_cmd_semantics.py` (new). SPEC.md:101-138.

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P1-01 | `test_assert_replaces_rc_check` | 104-107 | F1, F4 | `cmd: "echo running; false"`, `assert: running` passes; no sent line equals `echo __AUTOBOT_RC=$?` | pass |
| P1-02 | `test_errors_without_assert_replace_rc_check` | 105, 107 | F1, F4, `errors: ['% .*']` | `cmd: "false"` passes; no `echo __AUTOBOT_RC=$?` sent | pass |
| P1-03 | `test_no_assert_no_errors_nonzero_rc_raises` | 105 | F1, F4 | `cmd: "(exit 7)"` raises `RuntimeError` matching `exit code 7`; exactly one RC probe sent | pass |
| P1-04 | `test_rc_check_uses_last_line_only` | 105 | F1 | `cmd: ["false", "true"]` passes (only the last line's rc is checked) | pass |
| P1-05 | `test_assert_and_errors_errors_still_raise` | 101, 107 | F1, `errors: ['% .*']` | `cmd: "echo '% bad'; echo running"`, `assert: running` raises `CommandError` `command error: % bad` | pass |
| P1-06 | `test_assert_is_rendered` | 317 | F1, `vars: {want: running}` | `assert: "{{ vars.want }}"` on `echo running` passes; same step with `vars.want: nope` raises `assertion failed` | pass |
| P1-07 | `test_assert_any_of_list` | 104 | F1 | `assert: [absent, running]` on `echo running` passes; `assert: [absent, other]` raises `assertion failed` | pass |
| P1-08 | `test_assert_checks_output_of_all_lines` | 104 | F1 | `cmd: ["echo alpha", "echo beta"]`, `assert: alpha` passes | pass |
| P1-09 | `test_errors_match_multiline_anchor` | 116 | F1, `errors: ['^ERR']` | `printf 'ok\nERR x\n'` raises `command error: ERR x` (`^` matches line 2) | pass |
| P1-10 | `test_errors_dot_does_not_cross_lines` | 116 | F1, `errors: ['% .*']` | `printf '%% a\nb\n'` raises with message exactly `command error: % a` | pass |
| P1-11 | `test_errors_leave_session_at_prompt` | 116 | F1 `attached_runner`, F4 | `run_steps` of an erroring cmd raises; `sent.clear()`; next `cmd: echo next` (register) returns `next` and `sent.lines()` has no `""` solicit | pass |
| P1-12 | `test_register_value_is_stripped` | 127 | F1 | `printf '\n  x  \n\n'` registers `x` | pass |
| P1-13 | `test_register_with_errors_config_on_success` | 136 | F1, `errors: ['% .*']` | `echo fine` registers `fine` | pass |
| P1-14 | `test_unignored_failure_registers_nothing` | 136 | F1 `attached_runner`, `vars: {out: old}` | `cmd: "echo x; false"`, `register: out` raises; `vars.out == "old"` | pass |
| P1-15 | `test_ignored_assert_failure_registers_output` | 138 | F1 | `echo nope`, `assert: yes`, `ignore_error`, `register` → `nope`; next step runs | pass |
| P1-16 | `test_ignored_error_in_list_stops_at_failing_line` | 138 | F1, `errors: ['% .*']` | `cmd: ["echo a", "echo '% b'", "echo c"]` + ignore + register → `a\n% b`, and `c` never sent (F4). Covers the per-line stop with F4 evidence, which the existing test lacks. | pass |
| P1-17 | `test_session_before_not_clobbered_by_rc_probe` | 324 | F1 | `cmd: echo MARKX`, then `cmd: echo ran`, `register: r`, `when: "{{ session.before \| contains('MARKX') }}"` → `vars.r == "ran"`. Guards a naive fix of #5. | pass |
| P1-18 | `test_output_spanning_idle_poll_not_duplicated` | 111-114 | F1, `slow` | `cmd: "printf abc; sleep 6; echo def"`, register → `abc\ndef` | pass (was xfail #1) |
| P1-19 | `test_session_before_cleared_by_empty_output` | 324 | F1 `attached_runner` | `cmd: echo MARKX`; `cmd: "true"`; then `session.ctx["before"] == ""`, and a `when: "{{ session.before \| contains('MARKX') }}"` step is skipped | pass (was xfail #5) |
| P1-20 | `test_ignore_error_swallows_timeout` / `_eof` / `_template_error` (3 tests) | 118 | F1 (see the P1-20 deviation) | each with `ignore_error: true` and `register: r` (`vars.r` preset): timeout → `TimeoutError` propagates; EOF → `EOFError` propagates; undefined variable → `ValueError` `template error: ...` propagates. In all three the next step doesn't run and `vars.r` is unchanged. Also worth a row: a `responses exhausted` raised by a prompt wait inside the command propagates (it used to be swallowed) | pass (was todo #10) (x3) |

Totals: 22 pass (P1-01..20). Slow: P1-18.

## P2: `cmd` forms and embedded scripts

Files: `tests/test_cmd_forms.py` (new) and `tests/test_embedded_script.py` (extend). SPEC.md:99, 140-172.

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P2-01 | `test_multiline_skips_blank_lines` | 99 | F1, F4 | `cmd: "echo a\n\n   \necho b\n"` → `sent.commands() == ["echo a", "echo b"]` | pass |
| P2-02 | `test_each_line_waits_for_prompt` | 99 | F1, F5 | `cmd: ["echo a", "echo b"]` → timeline subsequence `get_prompt, sendline(echo a), get_prompt, sendline(echo b), get_prompt` | pass |
| P2-03 | `test_list_with_shebang_first_item_sent_verbatim` | 162 | F1, F4 | `cmd: ["#!/bin/false", "echo x"]` registers `x`; `#!/bin/false` sent as-is; no sent line contains `/tmp/_autobot_` | pass |
| P2-04 | `test_after_skips_initial_get_prompt` | 278 | F1, F5 | `line: "printf 'pre%s\\n' READY"`, then `cmd: echo x`, `after: preREADY` → no `get_prompt` between `expect([preREADY])` and `sendline(echo x)` | pass |
| P2-05 | `test_multiline_lines_rendered_individually` | 99, 317 | F1, `vars: {a: 1, b: 2}` | `cmd: "echo {{ vars.a }}\necho {{ vars.b }}"` registers `1\n2` | pass |
| P2-06 | `test_multiline_jinja_block_spanning_lines` | 99, 317 | F1 | `cmd: "{% for i in range(2) %}\necho n{{ i }}\n{% endfor %}"` registers `n0\nn1` | pass (was xfail #8) |
| P2-07 | `test_upload_chunks_at_most_512` | 165 | F1, F4, script > 3 x 512 b64 chars | each `printf %s <chunk>` payload ≤ 512 chars; chunk count = `ceil(len(b64)/512)`; concatenated chunks == `b64encode(rendered)` | pass |
| P2-08 | `test_upload_trailing_newline_preserved_once` | 165 | F1, F4; parametrize script with and without a final `\n` | decoded upload ends with exactly one `\n` | pass |
| P2-09 | `test_script_mode_700_and_staging_umask_077` | 166 | F1 | script `#!/bin/sh\nstat -c %a "$0" "$0.b64"\n` registers `700\n600` | pass |
| P2-10 | `test_embedded_script_is_rendered` | 162 | F1, `vars: {x: hi}` | `#!/bin/sh\necho {{ vars.x }}\n` registers `hi` | pass |
| P2-11 | `test_embedded_assert_failure_cleans_up` | 162, 171 | F1, `tmp_path_hex` | `assert: nomatch` raises `assertion failed`; script and `.b64` absent | pass |
| P2-12 | `test_byte_count_mismatch_fails_step` | 167 | F1, fake `wc` first on `PATH` (prints `1`) | raises `script upload .* failed`; files absent | pass |
| P2-13 | `test_byte_count_mismatch_ignorable` | 167 | as P2-12 + `ignore_error: true` | next step `echo next` runs and registers; files absent | pass |
| P2-14 | `test_cleanup_timeout_uses_shorter_step_timeout` | 172 | F1, `tmp_path_hex`, step `timeout: 2s`, script `trap '' INT; sleep 30` (so the interrupt can't end it and the cleanup wait runs into its bound), no constant patch | `TimeoutError`; total elapsed < 6 s (2 s step + ≤ 2 s cleanup + margin) | pass |
| P2-15 | `test_embedded_errors_patterns_apply` | 101, 162 | F1, `errors: ['% .*']`, `tmp_path_hex` | script printing `% bad` raises `CommandError`; files absent | pass |
| P2-16 | `test_timed_out_script_interrupted_and_removed` (parametrized: `ignore_error` false/true) | Embedded scripts: Cleanup | F1, F4, `tmp_path_hex`, script `sleep 30`, `timeout: 2s`, then `cmd: echo never` | `TimeoutError` propagates in both cases; elapsed < 6 s; `sent.controls() == ["c"]`; `rm -f <tmp> <tmp>.b64` sent; `echo never` not sent; files absent | pass |
| P2-17 | `test_timed_out_script_leaves_session_at_prompt` | Embedded scripts: Cleanup | F1 `attached_runner`, F4 | after the timed-out script step, `cmd: echo next` with `register` returns `next` in < 2 s (the script is no longer in the foreground); files absent | pass |
| P2-18 | `test_timed_out_upload_interrupted_and_removed` (parametrized: fake `wc` sleeps: hangs a chunk line; fake `base64` sleeps: hangs the decode) | Embedded scripts: Cleanup | F1, F4, fake first on `PATH`, `timeout: 2s` | `TimeoutError`; elapsed < 6 s; `^C` sent; the script path is never sent as a command; files absent | pass |
| P2-19 | `test_interrupt_failure_does_not_mask_timeout` | Embedded scripts: Cleanup | F1, `Session.sendcontrol` patched to raise `OSError` | `TimeoutError` (not `OSError`) propagates; stderr has `cleanup of <tmp> failed (OSError): boom` | pass |
| P2-20 | `test_no_interrupt_on_success_or_failure` | Embedded scripts: Cleanup | F1, F4 | success, exit code 2 and an `errors` match each run cleanup without `^C`: `sent.controls() == []` | pass |

Totals: 20 pass.

## P3: common step properties and templating context

File: `tests/test_common_props.py` (new). SPEC.md:272-334.

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P3-01 | `test_evaluation_order` | 288 | F1, F5 | `line: "printf 'pre%s\\n' READY"`; then `{line: "echo x", after: preREADY, when: "{{ session.before \| contains('pre') }}", delay_before: 1s, delay_after: 2s}` → timeline subsequence `expect([preREADY]), sleep(1), sendline(echo x), sleep(2)` | pass |
| P3-02 | `test_when_false_skips_delays_and_execution` | 288, 292 | F1, F5, F4 | `{line: "echo x", when: "false", delay_before: 1s, delay_after: 1s}` → no sleeps, `echo x` not sent | pass |
| P3-03 | `test_when_falsy_values` (parametrized) | 279, 292 | F1, F4 | `""`, `"false"`, `"False"`, `"0"`, `"none"`, `"{{ '' }}"`, `"false\n"` (Jinja drops a single trailing newline) → step skipped | pass |
| P3-04 | `test_when_truthy_values` (parametrized) | 292 | F1, F4 | `"true"`, `"True"`, `"1"`, `"yes"`, `"no"`, `"{{ 1 == 1 }}"` → step runs | pass |
| P3-05 | `test_when_none_value` / `test_when_surrounding_whitespace` (2 tests) | 292 | F1, `vars: {v: null}` | `when: "{{ vars.v }}"` (renders `None`) → skipped; `when: " false "` → skipped (also `FALSE`, `NONE`); `"no"`/`"off"` still run | pass (was todo #6) (x2) |
| P3-06 | `test_filter_contains` | 333 | `Runner(...).render` (no spawn) | `{{ 'abc' \| contains('b') }}` → `True`; `'x'` → `False`; non-string value is coerced | pass |
| P3-07 | `test_filter_search` | 334 | as P3-06 | `{{ 'v1.2' \| search('\\d+\\.\\d+') }}` → `True`; no match → `False` | pass |
| P3-08 | `test_range_global` | 327 | F1 | `cmd: "echo {{ range(3) \| list \| length }}"` registers `3` | pass |
| P3-09 | `test_after_sets_session_before_and_match` | 278, 324-325 | F1 `attached_runner` | `line: "printf 'Version: %s\\n' V42"`; `{cmd: "true", after: "V\\d+"}` → `ctx["match"] == "V42"`, `"Version: " in` the `before` seen by a `when` on the same step (`contains('Version')` runs) | pass |
| P3-10 | `test_template_context_env_vars_args` | 319-323 | F1, `env: {A: a}`, `vars: {b: b}`, cli args `{c: c}` | `echo {{ env.A }}-{{ vars.b }}-{{ args.c }}` registers `a-b-c` | pass |
| P3-11 | `test_sleep_rejects_common_props` (parametrized) | 274 | model + F7 | `{sleep: 1, <prop>: ...}` for each of after/when/delay_before/delay_after/timeout → model `extra_forbidden` and schema invalid | pass |
| P3-12 | `test_line_and_return_reject_timeout` | 284 | model + F7 | `{line: x, timeout: 1}` and `{return: 1, timeout: 1}` rejected by both | pass |
| P3-13 | `test_non_sleep_steps_accept_common_props` (parametrized over cmd/call/block/line/return/control) | 274-282 | model + F7 | each accepts after/when/delay_before/delay_after (+ timeout where allowed) in both | pass |
| P3-14 | `test_step_timeout_overrides_default` | 282 | F1 | `{cmd: "sleep 3", timeout: 1}` raises `TimeoutError` in < 2.5 s | pass |
| P3-15 | `test_call_honors_when_and_delays` | 274 | F1, F5, `fn: {f: [cmd: echo inf, register: r]}` | `{call: f, when: "false"}` → `r` unset; `{call: f, delay_before: 1s}` → `sleep(1)` before `sendline(echo inf)` | pass |
| P3-16 | `test_block_when_false_skips_prompt_swap` | 274 | F1, F5 | block with prompts and `when: "false"` → no `restore_handlers` in timeline, enter not run | pass |
| P3-17 | `test_after_timeout_uses_step_timeout` | 278, 282 | F1 | `{cmd: "true", after: NEVER, timeout: 1}` → `TimeoutError` `timed out after 1.0s waiting for the after pattern 'NEVER'` in < 2.5 s | pass |
| P3-18 | `test_after_eof_names_pattern` | `cmd` ignore_error (closed connection) | F1; `line: exit`, then `{cmd: "true", after: "login: {{ 'x' }}", timeout: 5}` | `EOFError` exactly `connection closed while waiting for the after pattern 'login: x'` (the rendered pattern) | pass |

Totals: 19 pass (P3-01..18).

## P4: `get_prompt`, prompts and credential cycling

File: `tests/test_get_prompt.py` (new). SPEC.md:33-53, 336-346. Session-level tests build handlers through `Runner.build_handler` so that prompt models go through the real path.

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P4-01 | `test_get_prompt_never_sends_command` | 338 | F1 `shell_session`, F4 | after reaching the first prompt, `sendline("echo hi")` and `sent.clear()`, `get_prompt()` returns and `sent.lines() == []` | pass |
| P4-02 | `test_shell_prompt_forms` (parametrized: `return: true`; no `send`. The `return: true` case had a `send`, which is `return_with_send` since 2026-10) | 38, 341 | F2 `--wait-enter --then prompt`, F4 | returns at `PROMPT$ ` with nothing sent after the kick | pass |
| P4-03 | removed: `send: []` is now `send_list` (P6-52); an empty `sendEach` collection (P4-23) is the only `no response available` | | | | removed |
| P4-04 | `test_solicit_newline_after_idle` | 343 | F2 `--wait-enter`, F4, `slow` | no manual kick; `get_prompt(timeout=15)` returns; `sent.lines() == [""]`; elapsed ≥ 5 s | pass |
| P4-05 | `test_solicit_newline_only_once` | 343 | F2 `--silent`, F4, `slow` | `get_prompt(timeout=11)` raises `TimeoutError`; exactly one `""` sent | pass |
| P4-06 | `test_no_solicit_after_handler_fired` | 343 | F2 `--order login --post-auth-delay 6`, F4, `slow`, simple prompt `expect: [login:]`, `send: admin` | returns at the shell prompt; `sent.lines() == ["admin"]` (no `""`) | pass |
| P4-07 | `test_timeout_error_at_deadline` | 344 | F2 `--silent` | `get_prompt(timeout=1)` raises `TimeoutError` exactly `timed out after 1s waiting for a shell prompt ('sh')` within 2 s | pass |
| P4-08 | `test_eof_error_on_child_exit` | 336 | F2 `--wait-enter --exit-after-banner` | kick, then `get_prompt` raises `EOFError` (the message is checked by P4-36) | pass |
| P4-09 | `test_eof_is_not_timeout` | 336 | as P4-08 | elapsed < 2 s (EOF is seen before any poll timeout) | pass |
| P4-10 | removed with the flat-list `send` form (P4-12 is the `sendEach` login-then-password case) | | | | removed |
| P4-11 | removed with the list-of-lists `send` form (P4-12 covers credential cycling with `sendEach`) | | | | removed |
| P4-12 | `test_send_each_with_fields` | `sendEach` | F2 `--accept admin:pass2`, `vars.creds` with two items and `fields` entries `login:` → `username`, `Password:` → `password` (no `expect`) | log shows attempt 1 `pass1`, attempt 2 `pass2`; returns at the shell | pass |
| P4-13 | `test_send_each_without_fields_stringifies` | 53 | F2 `--order password --accept :1234`, `vars.pins: [1111, 1234]`, `each: vars.pins` | log `PASSWORD=1111`, `PASSWORD=1234` (ints sent as strings) | pass |
| P4-14 | `test_responses_exhausted` | Response selection | F2 accepts nothing, `sendEach` `fields` over one item `admin`/`bad` | `RuntimeError` matching `prompt 'login': responses exhausted` | pass |
| P4-15 | `test_handlers_reset_per_get_prompt` | Response selection | F2 `--repeat 2 --accept admin:secret`, `sendEach` `fields` over one item `admin`/`secret` | two consecutive `get_prompt` calls (with `sendline("again")` between) both succeed; log has two `LOGIN=admin` | pass |
| P4-16 | removed with grouped `expect` entries (P4-12 is the `sendEach` login-first case) | | | | removed |
| P4-17 | `test_fields_password_first` and four more | Response selection | F2 `--order password,login`, `sendEach` with `fields` entries `login:` → `username`, `Password:` → `password` | `--accept admin:secret`, one item → log `ENTER=, PASSWORD=secret, LOGIN=admin`; returns at the shell. Extra rows (SPEC's table): two items `p1`/`p2` with `--accept admin:p2` → `PASSWORD=p1, LOGIN=admin, PASSWORD=p2, LOGIN=admin`; `--order password` → one password per set; one item `bad` → `PASSWORD=bad, LOGIN=admin`, then `responses exhausted`; login-first, two sets, a third `login:` → `responses exhausted`. These were the grouped literal-`send` tests, migrated to `sendEach`; the one-item literal set case (`no response available for 'Password:'`) was removed with that message | pass (was todo #4) |
| P4-18 | `test_login_prompt_in_same_chunk_as_banner` | 81, 341 | F2 `--same-chunk --order login,password`, no kick, `sendEach` `fields` over one item `admin`/`secret` | `get_prompt()` returns at the shell; log is exactly `LOGIN=admin`, `PASSWORD=secret`; `sent.lines()` has no `""` | pass (was xfail #3) |
| P4-19 | `test_shell_prompt_in_same_chunk_as_banner` | 81, 341 | F2 `--same-chunk --then prompt`, no kick | `get_prompt()` returns in < 4 s (no solicit wait) | pass (was xfail #3) |
| P4-20 | `test_send_template_rendered_at_send_time` | prompts | simple prompt `expect: ['Name\? ']`, `send: '{{ vars.user }}'`; script `cmd: echo admin` `register: user`, then `line: <F2 spawn cmd --order none --ask Name?>`, then `cmd: "true"` | `Runner(cfg, {})` does not raise; F2 log exactly `ASK=admin` | pass (was xfail #7) |

Also check in P4-01: `session.ctx["match"] == "PROMPT$ "` after a shell-prompt match (SPEC.md:294). This is folded into P4-01, not counted separately.

| P4-21 | `test_send_each_nested_path_ignores_other_keys` | `sendEach` | F2 `--accept admin:pass2`, `each: vars.site.creds`, `fields` entries for `username` and `password`, the first item has an extra `note` key | same log as P4-12 (only the named fields are sent) | pass |
| P4-22 | `test_send_each_scalar_items_without_fields` | `sendEach` | F2 `--order password --accept :True`, `vars.pins: ["1111", 2.5, true]`, no `fields` | log `PASSWORD=1111`, `PASSWORD=2.5`, `PASSWORD=True` | pass |
| P4-23 | `test_send_each_empty_list_fails_at_send_time` | `sendEach`, Response selection | F2, `vars.creds: []`, one `fields` entry `login:` → `username` | the `Runner` builds; `get_prompt` raises `RuntimeError` `prompt 'login': no response available` | pass |
| P4-24 | `test_send_each_match_alternatives_send_the_same_field` | `sendEach`, Response selection | F2 `--order login,password --accept pw1:pw2`, one `fields` entry `match: [login:, Password:]` → `password`, two items | log `ENTER=, LOGIN=pw1, PASSWORD=pw2`: both alternatives send `password`, and they share one fired state (the second match advances) | pass |
| P4-25 | `test_send_each_without_fields_patterns_share_one_state` | `sendEach`, Response selection | F2 `--order login,password --accept 1111:2222`, `expect: [login:, Password:]`, `each: vars.pins` without `fields`, pins `[1111, 2222]` | log `ENTER=, LOGIN=1111, PASSWORD=2222`: any later match advances (per-pattern state would send `1111` again) | pass |
| P4-26 | `test_simple_prompt_answered_each_time_it_appears` | Response selection | F2 `--order none --then prompt`, `--ask continue?` three times; simple prompt `expect: ['continue\?', 'are you sure\?']`, `send: 'yes'` | log `ENTER=, ASK=yes, ASK=yes, ASK=yes`: any match sends the string, each time the prompt appears, no exhaustion | pass |
| P4-27 | `test_simple_prompt_alternatives` | prompts | as P4-26, asks `are you sure?` then `continue?` | log `ENTER=, ASK=yes, ASK=yes`: each alternative sends the string | pass |
| P4-28 | `test_simple_prompt_single_expect_string` | prompts | as P4-26 with `expect: 'continue\?'` (a string), one ask | log `ENTER=, ASK=yes` | pass |
| P4-29 | `test_simple_prompt_template` | prompts | as P4-26, `send: '{{ vars.answer \| upper }}'`, `vars.answer: y`, two asks | log `ENTER=, ASK=Y, ASK=Y` (rendered on each send) | pass |
| P4-30 | `test_simple_prompt_empty_send_presses_enter` | prompts | as P4-26, `send: ''`, one ask, F4 | `sent.lines() == [""]`; log `ENTER=, ASK=` | pass |
| P4-31 | `test_simple_prompt_undefined_variable_aborts` | prompts | as P4-26, `send: '{{ vars.nope }}'`, one ask, no `--then` | `ValueError` `prompt 'confirm': template error: ...`; nothing sent after the kick | pass |
| P4-32 | `test_prompt_text_checked_like_a_prompt_wait` (parametrized: same, anchored, suffix, other, no handlers, a `send` regex starting first, ties both ways, escape skipped / starting first / inside the prompt, line break skipped, invalid regex) | block, "Prompt state across a swap" | `Session(handlers)._is_shell_prompt(text)`, no spawn | true only when the earliest match (ties: the first defined) after skipping `\r\n` and escape sequences is a shell prompt; an invalid regex gives false instead of raising | pass (#14) |
| P4-33 | `test_swap_keeps_a_recognized_prompt` (parametrized: same, `[A-Z]+\$ `, an added question handler, `BLK\$ `) | block, "Prompt state across a swap" | F1 `shell_session`, F4; reach a prompt after `echo hi`, `sent.clear()`, `restore_handlers(...)` | recognized: `get_prompt` returns `""` in < 0.5 s and sends nothing; `BLK\$ `: `get_prompt(timeout=1)` raises `TimeoutError` after exactly one solicit `""`; `session.before`/`match` stay `hi\n`/`PROMPT$ ` | pass (#14) |
| P4-34 | `test_swap_never_starts_prompt_state` | block, "Prompt state across a swap" | F1 `shell_session`; `sendline("echo hi")` without a wait, then a swap to a matching handler | `get_prompt` returns `hi\n` (it read the output instead of returning `""` at once) | pass (#14) |
| P4-35 | `test_timeout_names_shell_prompts` (parametrized: `names` = shell `sh`, answer-only `q`, shell `root`; `none` = `q` only) | get_prompt, step 4 | raw `Session` on bash with `PS1='X> '` (no handler matches) | `get_prompt(timeout=1)` raises `TimeoutError` exactly `timed out after 1s waiting for a shell prompt ('sh', 'root')` / `... (none defined)` | pass (x2) |
| P4-36 | `test_eof_names_shell_prompts` (parametrized as P4-35) | get_prompt, step 5 | as P4-35, then `sendline("exit")` | `get_prompt(timeout=10)` raises `EOFError` exactly `connection closed while waiting for a shell prompt ('sh', 'root')` / `... (none defined)` (the same names as the timeout) | pass (x2) |

Totals: 32 rows pass (P4-01..36 without the removed P4-03, P4-10, P4-11, P4-16). Slow: P4-04, 05, 06. P4-18 lost its `slow` marker: with #3 fixed it no longer waits for the idle poll (about 0.2 s).

## P5: attach and block lifecycles, env

Files: `tests/test_lifecycle.py` (extend) and `tests/test_env.py` (new). SPEC.md:20-31, 68-85, 186-204; README.md:100, 304.

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P5-01 | `test_attach_lifecycle_order` | 79-85 | F1, F5, `children`; each phase appends a tag to `tmp_path/log` (prepare via `echo prepare >> log`, others via `cmd`) | log is `prepare, attach, main, breakout`; timeline has `attach` after prepare; child closed after breakout | pass |
| P5-02 | `test_prepare_nonzero_aborts_before_spawn` | 72, 80 | F5, `children`, prepare `#!/bin/sh\nexit 3` | `RuntimeError` `prepare script failed with exit code 3`; no `attach` in timeline; `children == []` | pass |
| P5-03 | `test_prepare_temp_file_removed` (parametrized success / failure) | 72 | `monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))` | no `_autobot_*` left in `tmp_path` | pass |
| P5-04 | `test_prepare_without_shebang_runs_under_sh` | attach `prepare` | `prep_tmp`, F5; prepare `\n  echo "sh:$0" > out` (no shebang, leading blank line and spaces) | `out` is `sh:<tmp>/_autobot_...` (run as `/bin/sh <tmp>`); first `>> ` lines `prepare: running local script (no shebang, using /bin/sh)`, `prepare: done`; spawn follows; temp file removed | pass (was: raises `OSError` `Exec format error`) |
| P5-05 | `test_prepare_is_templated` / `test_prepare_bash_array_length_literal` (2 tests) | 72, 317 | prepare `echo {{ env.X }} > out` / `arr=(a b); echo ${#arr[@]} > out` | `out` holds the rendered value / bare `${#arr[@]}` → `ValueError` `template error: ...` from `run()` before spawn (no child, temp file removed); wrapped in `{% raw %}...{% endraw %}` → `out` is `2` | pass (was todo #9) (x2) |
| P5-06 | `test_attach_breakout_runs_after_script_failure` | 77, 84 | F1, breakout `cmd: "touch <tmp>/bo"`, main `cmd: false` | raises `exit code 1`; `bo` exists | pass |
| P5-07 | `test_attach_breakout_runs_after_attach_script_failure` | 76-84 | F1, `attach_script: [cmd: false]` | raises; main steps not sent (F4); breakout marker exists | pass |
| P5-08 | `test_attach_breakout_resets_handlers_first` | 84 | F1, F5 | `reset_handlers` appears in timeline before the first breakout `sendline` | pass |
| P5-09 | `test_attach_breakout_error_logged` | 84 | F1, breakout `[cmd: false]`, `capsys` | run returns normally; stderr contains `breakout error (RuntimeError)` | pass |
| P5-10 | `test_block_prompts_active_inside_block` | 193, 199 | F1, F6 `ProbeExecutor`; block prompts `[{name: blk, expect: ['BLK\$ '], return: true}]`, enter `cmd: "PS1='BLK$ '"`, script `[probe, cmd: echo x]`, breakout `line: "PS1='PROMPT$ '"` | probe saw handlers `["blk"]`; after the block, the next top-level `cmd: echo y` succeeds and handlers are `["sh"]` | pass |
| P5-11 | `test_block_prompts_restored_on_failure` (parametrized: enter fails, script fails; each with and without breakout) | 203, README:304 | F1 `attached_runner` | raises the original error; handler names back to `["sh"]` | pass |
| P5-12 | `test_nested_block_restore` | 193, 203 | F1, F6 probe | outer `[o]`, inner `[i]`; probe inside inner sees `["i"]`, probe after inner sees `["o"]`; after the outer block `["sh"]`; the same with the inner script failing | pass |
| P5-13 | `test_block_without_prompts_leaves_handlers_untouched` | 199, 203 | F1, F6 probe, F5 | the probe's handler list is the same object (`id`) as the top-level list; no `restore_handlers` in timeline | pass |
| P5-14 | `test_block_breakout_resets_handlers_first` | 202 | F1, F5 | `reset_handlers` before the first block-breakout `sendline` | pass |
| P5-15 | `test_block_step_order` | 198-203 | F1, markers via `cmd: echo tag >> log` | log is `enter, script, breakout` | pass |
| P5-16 | `test_block_breakout_error_does_not_restore_early` | 203, README:304 | F1, block prompts, breakout `[cmd: false]`, `capsys` | step completes; `block breakout error (RuntimeError)` logged; handlers `["sh"]` | pass |
| P5-17 | `test_attach_env_replaces_parent_env` | 75 | F1, `monkeypatch.setenv("AUTOBOT_LEAK", "1")` | `echo "leak=${AUTOBOT_LEAK:-unset}"` registers `leak=unset` | pass |
| P5-18 | `test_attach_env_default_when_omitted` | 75 | F4 `spawned` in raise-after-record mode, attach without `env` | spawn kwargs `env == {"TERM": "dumb", "NO_COLOR": "1"}` | pass |
| P5-19 | `test_yaml_env_overridden_by_os_env` | 25 | `monkeypatch.setenv("AB_X", "os")`, `env: {AB_X: yaml, AB_Y: keep}` | `runner.render("{{ env.AB_X }}-{{ env.AB_Y }}") == "os-keep"` (no spawn) | pass |
| P5-20 | `test_env_nesting` | 25 | `env: {A: a, B: "{{ env.A }}-b", C: "{{ env.B }}-c"}` | `env.C == "a-b-c"` | pass |
| P5-21 | `test_env_nesting_uses_os_override` | 25 | `setenv("A", "os")`, same env | `env.C == "os-b-c"` | pass |
| P5-22 | `test_env_cycle_detected` (parametrized: self `A: "{{ env.A }}x"`; mutual `A: "{{ env.B }}"`, `B: "{{ env.A }}"`; mutual with a growing value; 3-cycle; a lead-in key `X -> A` outside the cycle; `env['B'] \| upper` / `env.get('A')` forms) | 25 | none | `Runner(...)` raises `ValueError` exactly `env cycle: A -> ... -> A`, naming only the cycle | pass (was `nesting too deep`; mutual cycles passed silently) |
| P5-23 | `test_attach_timeout_subsecond` (parametrized `500ms` → 0.5, `1.5s` → 1.5) | 74, 307-313 | F5 (`Session.attach` recorder, not delegating) | recorded `timeout` equals the parsed float | pass (was xfail #2) |
| P5-24 | `test_attach_timeout_default_and_spawn_templated` | 73-74, 317 | F5 recorder, `spawn: "{{ env.SH }}"`, no `timeout` | recorded spawn is the rendered string; `timeout == 300` | pass |
| P5-25 | `test_block_send_each_error_aborts_at_entry` (parametrized: `vars.nope`; `vars.creds` overwritten by an earlier `register`) | `sendEach` | F1, block prompt `sendEach` with `fields` entries; `enter`, `script`, block breakout, a later step and the attach breakout each append a tag to `tmp_path/log` | `run()` raises `ValueError` exactly `prompt 'login': sendEach '<each>': no key 'nope' in 'vars'` / `'vars.creds' is a string, not a list`; log is only `attach-breakout`; handlers `["top"]` | pass |
| P5-26 | `test_block_send_each_valid` | `sendEach` | F1, F6 probe, same block with a valid `vars.creds` | log `enter, script, block-breakout`; the probe inside sees `["blk", "login"]`; handlers `["top"]` afterwards | pass |
| P5-27 | `test_env_nesting_any_order_and_forms` | 25 | `env: {C: "{{ env['B'] \| upper }}-c", B: "{{ env.get('A') }}-b-{{ args.a }}", A: "{{ vars.v }}", X: "{{ env.NOPE \| default('dflt') }}-{{ 'A' in env }}"}` (referencing keys before the keys they reference) | `render("{{ env.C }} {{ env.X }}") == "A-B-ARG-c dflt-True"` | pass |
| P5-28 | `test_env_os_override_breaks_cycle` | 25 | `setenv("A", "os")`, `env: {A: "{{ env.B }}", B: "{{ env.A }}-b"}` | no error; `env.A == "os"`, `env.B == "os-b"` | pass |
| P5-29 | `test_env_value_rendered_once` | 25 | `env: {A: "{% raw %}{{ lit }}{% endraw %}", B: "{{ env.A }}"}` | `env.B == "{{ lit }}"` (the rendered text isn't rendered again) | pass |
| P5-30 | `test_env_undefined_key_and_depth_limit` | 25 | `env: {A: "{{ env.NOPE }}"}`; chains of 50 and 51 keys | `ValueError` exactly `template error: env has no key 'NOPE'`; 50 keys resolve; 51 raise exactly `env nesting deeper than 50 levels: K0 -> ... -> K50` | pass |
| P5-31 | `test_env_os_value_is_verbatim` | 25 | `setenv("A", "p{{w}}d{% x %}")`, `env: {A: default, B: "{{ env.A }}-b"}`; then `setenv("A", "{{ env.B }}")` | no error; `env.A == "p{{w}}d{% x %}"`, `env.B == "p{{w}}d{% x %}-b"`; with `A={{ env.B }}` no cycle: `env.A == "{{ env.B }}"`, `env.B == "{{ env.B }}-b"` | pass (behavior change: OS values were rendered) |
| P5-32 | `test_spawn_failure_skips_scripts_and_breakout` (parametrized: `timeout` = `sleep 30` with `attach.timeout: 1`; `exits` = `true`; `not-found` = `autobot_no_such_cmd`) | attach, "If the spawn wait fails" | F1, F4 `sent`, `children`, `capsys`; prepare, `attach.script`, `script` and the breakout each append a tag to `tmp_path/log`, and the breakout also sends `line: exit` | raises `TimeoutError` exactly `timed out after 1.0s waiting for the first output from 'sleep 30' (attach.timeout)` / `EOFError` exactly `connection closed before any output from 'true' (exit status 0)` / `pexpect.ExceptionPexpect` `The command was not found or was not executable: ...`; log is only `prepare`; nothing was sent; `_cld is None`; the child is dead and `closed` (`not-found`: no child at all); the `>> ` lines are exactly `prepare: running local script`, `prepare: done`, `attach: <spawn>` | pass (replaces `test_attach_initial_timeout_closes_child`) |
| P5-33 | `test_banner_then_exit_runs_breakout` | attach, "Any output ends the spawn wait" | F2 `--exit-after-banner`, the same tags, `children`, `capsys` | raises `EOFError` exactly `connection closed while waiting for a shell prompt ('sh')`; log is only `prepare`; the child is dead and `closed`; the `>> ` lines are the P5-32 lines then `breakout: detaching`, `breakout error (EOFError): connection closed while waiting for a shell prompt ('sh')` | pass |
| P5-34 | `test_spawn_failure_cli_exit_status` (parametrized: the three P5-32 cases and `banner` = F2 `--exit-after-banner`) | attach; CLI, "Errors after the script is loaded" | `run_cli`; the `timeout` case spawns `sleep 9<pid>`, so a leaked child can be found with `pgrep -x -f` (and is killed in `finally`) | status 1; `Traceback` on stderr, last line `<error>: ...`; `>> breakout: detaching` only for `banner`; log is only `prepare`; no `sleep 9<pid>` left running | pass |
| P5-35 | `test_block_swap_keeps_recognized_prompt` (parametrized: block prompts `PROMPT\$ `, `[A-Z]+\$ `, `PROMPT\$ ` plus a question handler) | block, "Prompt state across a swap" | F1, F4; `cmd`s with `register` before, inside (two) and after the block | run < 3 s (was about 10.7 s: a 5 s wait on entry and on exit); no `""` sent; commands exactly `echo before, echo in1, echo in2, echo after`; every `register` right; `session.before`/`match` end as `after\n`/`PROMPT$ ` | pass (#14) |
| P5-36 | `test_block_question_answered_without_solicit` | block, "Prompt state across a swap" | F1, F4; block adds `yn` (`continue\? ` → `y`); `read -p 'conti''nue? ' a; echo got=$a` | run < 3 s; sent lines exactly `echo before, RC, <read>, y, RC, echo after, RC`; `vars.ans == "y\ngot=y"` | pass (#14) |
| P5-37 | `test_block_prompt_change_waits_for_new_prompt` | block, "Prompt state across a swap" | F1, F4; block prompts `BLK\$ ` only, `enter: line PS1='BL''K$ '`, breakout `line PS1='PROM''PT$ '` | run < 3 s; no `""` sent; registers before, in and after the block right | pass (#14) |
| P5-38 | `test_nested_blocks_keep_recognized_prompt` | block, "Prompt state across a swap" | F1, F4, F6 probe; outer `PROMPT\$ `, inner `[A-Z]+\$ `, `cmd`s at each level | run < 3 s; no `""` sent; all registers right; the probes see `session.before` `o1\n` inside the inner block and `i1\n` after it | pass (#14) |
| P5-39 | `test_unrecognized_prompt_not_used_on_entry` | block, "Prompt state across a swap" | F1, F4; block prompts `BLK\$ ` only, `enter: cmd PS1='BL''K$ '` with `timeout: 1`; attach breakout `cmd: echo bo` (register) | `TimeoutError` exactly `timed out after 1.0s waiting for a shell prompt ('blk')`; sent lines exactly `echo before, RC, "", echo bo, RC` (the `PS1` command is never sent); `vars.bo == "bo"` | pass (#14) |
| P5-40 | `test_unrecognized_prompt_not_used_on_exit` | block, "Prompt state across a swap" | F1, F4; block prompts `BLK\$ `, entered with `line`, no breakout; then `cmd: echo after` with `timeout: 1` | `TimeoutError` exactly `timed out after 1.0s waiting for a shell prompt ('top')`; sent lines exactly `PS1=..., echo in1, RC, ""` (`echo after` is never sent) | pass (#14) |
| P5-41 | `test_restore_after_error_keeps_recognized_prompt` | block, "Prompt state across a swap" | F1, F4; block prompts `PROMPT\$ `, script `cmd: false`, block breakout and attach breakout each `cmd` with `register` | `exit code 1` raised in < 3 s; no `""` sent; both breakout registers right | pass (#14) |
| P5-42 | `test_prepare_shebang_after_leading_junk` (parametrized: junk `blank-lines` `\n\n\r\n`, `spaces` ` \t `, `bom` U+FEFF, `mixed` BOM, newline, space, U+200B, tab, U+2060; interpreter `#!/bin/sh` / `#!/usr/bin/env python3`) | attach `prepare` | `prep_tmp`, `capsys` | `out` is `sh` / `6` (a Python-only `sum(range(4))`, so the shebang chose python3); the progress line is the plain `>> prepare: running local script`; temp file removed | pass (was `OSError` `Exec format error`) (x8) |
| P5-43 | `test_prepare_shebang_crlf` | attach `prepare` | `prep_tmp`; prepare `#!/bin/sh\r\necho ok > out` | `out` is `ok`; temp file removed | pass (was `FileNotFoundError` for `/bin/sh\r`) |
| P5-44 | `test_prepare_fallback_nonzero_aborts` (parametrized: `no-shebang` `echo hi; exit 4`; `junk-shebang` BOM, newline, spaces, `#!/bin/sh`, `exit 4`) | attach `prepare` | `prep_tmp`, F5, `children` | `RuntimeError` exactly `prepare script failed with exit code 4`; temp file removed; no `attach`; `children == []` | pass (x2) |
| P5-45 | `test_prepare_interpreter_cannot_run` (parametrized: `missing` interpreter; `not-executable` mode 600 file) | attach `prepare` | `prep_tmp`, F5, `children`; prepare `\n#!<interp>\r\necho hi` | `RuntimeError` exactly `prepare script could not run ('#!<interp>'): [Errno 2] No such file or directory` / `[Errno 13] Permission denied`; `__cause__` is `FileNotFoundError` / `PermissionError`; temp file removed; no `attach`; `children == []` | pass (x2) |
| P5-46 | `test_attach_timeout_message_names_spawn` | attach, "If the spawn wait fails" | raw `Session`, `attach("sleep 30", timeout=0.5)` | `TimeoutError` exactly `timed out after 0.5s waiting for the first output from 'sleep 30' (attach.timeout)` (no `(?=.)`); `__cause__` is `pexpect.TIMEOUT` | pass |
| P5-47 | `test_check_rc_timeout_message` | `cmd` ignore_error (timeouts) | F1 `shell_session`; reach a prompt, `sendline("sleep 30")` | `check_rc(timeout=1)` raises `TimeoutError` exactly `timed out after 1s waiting for the exit code of the command (echo $?)` (no `__AUTOBOT_RC` regex) | pass |
| P5-48 | `test_expect_timeout_message` (parametrized: `patterns` = no `what`; `what` = `the after pattern 'NEVER'`) | Common step properties, `after` | F1 `shell_session`; `expect(..., timeout=0.5, what=...)` | `timed out after 0.5s waiting for 'NEVER' or 'NOPE'` (patterns as written, no list repr) / `... waiting for the after pattern 'NEVER'` | pass (x2) |
| P5-49 | `test_attach_eof_message` (parametrized: `true`, `false`, `sh -c 'exit 255'`, `sh -c 'kill -KILL $$'`, `sh -c 'kill -HUP $$'`) | attach, "If the spawn wait fails" | raw `Session`, `attach(<spawn>, timeout=5)`; each process exits before any output | `EOFError` exactly `connection closed before any output from '<spawn>'` plus ` (exit status 0)` / ` (exit status 1)` / ` (exit status 255)` / ` (killed by SIGKILL)` / nothing for `SIGHUP` (closing the pty sends it too); `__cause__` is `pexpect.EOF`; `_cld is None` | pass (x5) |
| P5-50 | `test_check_rc_eof_message` | `cmd` ignore_error (closed connection) | F1 `shell_session`; reach a prompt, `sendline("sleep 0.5; exit")`, so the shell is alive when the check is sent | `check_rc(timeout=5)` raises `EOFError` exactly `connection closed while waiting for the exit code of the command (echo $?)` | pass |
| P5-51 | `test_sleep_eof_message` | `sleep` | F1; `line: exit`, then `sleep: 5s` | `EOFError` exactly `connection closed while waiting for the end of a sleep` | pass |

Totals: 52 pass.

## P6: model, schema, example and CLI parity

Files: `tests/test_models.py`, `tests/test_schema_parity.py`, `tests/test_examples.py`, `tests/test_cli.py` (all new). SPEC.md:12-31, 33-53, 307-313, 348-355.

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P6-01 | `test_yaml_aliases` | 38, 104, 125, 258 | model | `assert` → `assert_`, `register` → `register_`, prompt `return` → `is_shell_prompt`, step `return` → `newline_count` | pass |
| P6-02 | `test_python_field_names_rejected` | 18 | model | `{cmd: x, assert_: y}`, `{cmd: x, register_: y}` → `extra_forbidden` | pass |
| P6-03 | `test_extra_keys_forbidden_everywhere` (parametrized over top-level, attach, breakout, prompt, sendEach, fn, block, every builtin step) | 18 | model | unknown key → `extra_forbidden` | pass |
| P6-04 | `test_required_fields` (parametrized) | 22-31, 70-73, 190-192 | model | missing `autobot` / `attach` / `script` / `attach.spawn` / prompt `name` / `expect` / block `name` → `missing` | pass |
| P6-05 | `test_version_pattern`, `test_old_version_points_to_migration` | Top-level fields | model | `2026-10` ok; `2026-11`, `2026-8`, `26-10`, `2026/10`, `202610`, ` 2026-10` → one `unsupported_version` error at `autobot`, `unsupported autobot version '<v>'; expected 2026-10`; `2026-08` → `autobot 2026-08 is no longer supported; use 2026-10 (see "Migrating from 2026-08" in SPEC.md)` | pass |
| P6-06 | `test_step_discrimination` | 87-270 | model, F6 | each builtin key → its model class; registered plugin key → `PluginStep`; `{cmd: a, line: b}` → `extra_forbidden` | pass |
| P6-07 | `test_send_forms`, `test_send_each_fields_form` | prompts, `sendEach` | model | a `send` string and a `sendEach` without `fields` accepted with `expect`; `sendEach` with `fields` entries accepted without `expect` (`match` is stored as a list). The rejected forms are P6-52 | pass |
| P6-08 | removed: `expect` forms are P6-52 (a regex or a list of regexes accepted, a grouped or mixed entry rejected) | | | | removed |
| P6-09 | `test_duration_values` (parametrized) | 307-313 | model via `sleep` | `5`→5.0, `5s`, `500ms`→0.5, `2m`→120, `1h`→3600, `1.5s`→1.5, `1.5`→1.5 accepted; `"5"`, `5x`, `""`, `ms`, `1 s`, `-1s` rejected | pass |
| P6-10 | `test_schema_is_valid_draft_2020_12` | 12 | F7 | `Draft202012Validator.check_schema(schema)` | pass |
| P6-11 | `test_parity_accept_corpus` (parametrized) | 12 | F7 `both_validate` | a corpus of valid docs (minimal; every step type; a `send` string and `send: ''`; `sendEach` with `fields` entries and no `expect`; a nested `sendEach` path; `expect` as a string; nested block; fn; each duration form) → `(True, True)` | pass |
| P6-12 | `test_parity_reject_corpus` (parametrized) | 12 | F7 | invalid docs (missing required, extra keys, bad version, bad duration, sleep+when, line+timeout, mixed send, a `sendEach` path not of the form `vars.<key>...`, `autobot` `2026-08` or `2026-11`, the old `fields: [u, p]` list) → `(False, False)` | pass |
| P6-13 | `test_parity_mixed_expect` | prompts | F7 | `expect: ["a", ["b", "c"]]` → `(False, False)`: every `expect` entry is a single regex | pass (was xfail #11, then accepted until grouped entries were removed) |
| P6-14 | `test_parity_version_trailing_newline` | 24 | F7 | `autobot: "2026-10\n"` → `(False, False)`: the schema's `const` compares whole strings (the old `pattern` let Python's `re.search` accept it, so only the model side was asserted). | pass (was xfail #11) |
| P6-15 | `test_parity_return_minimum` | 258-263 | F7 | `return: 0`, `-2`, `true` and `"1"` → `(False, False)` | pass (was todo #11) |
| P6-16 | `test_parity_negative_duration` | 307-313 | F7 | `sleep: -1` → `(False, False)`; model message `invalid duration` | pass (was todo #11) |
| P6-17 | `test_parity_bool_duration` | 307-313 | F7 | `sleep: true` → `(False, False)`; model message `invalid duration` | pass (was todo #11) |
| P6-18 | `test_parity_fn_script_required` | 57 | F7 | `fn: {f: {}}` → `(False, False)`; model type `missing` at `fn.f.script` | pass (was todo #11) |
| P6-19 | `test_examples_validate_model` (parametrized over `examples/*.yaml`) | 12 | model | `Config.model_validate(yaml.load(..., Loader=cli.UniqueKeyLoader))` succeeds (the CLI loader, so a duplicate key in an example fails) | pass |
| P6-20 | `test_examples_validate_schema` (parametrized) | 12 | F7 | schema accepts each example, loaded with `cli.UniqueKeyLoader` | pass |
| P6-21 | `test_cli_arg_passed_to_templates` | 351-355 | CLI subprocess, F1 script, `--arg msg=hi` | `cmd: "echo got-{{ args.msg }}"` → `got-hi` in stdout, rc 0 | pass |
| P6-22 | `test_cli_arg_value_may_contain_equals` | 355 | as P6-21, `--arg k=a=b` | `got-a=b` | pass |
| P6-23 | `test_cli_arg_without_equals_is_clean_error` | 355 | CLI, `--arg bad` | rc 1; stderr `--arg requires KEY=VALUE`; no `Traceback` | pass |
| P6-24 | `test_cli_non_mapping_yaml_is_clean_error` (parametrized: empty file, top-level list) | 12, 18 | CLI | rc 1; stderr mentions validation; no `Traceback` | pass (was xfail #18) |
| P6-25 | `test_cli_unreadable_script_is_clean_error` (parametrized: default and `run` forms × missing, directory, no permission) | CLI | CLI subprocess | rc 1; empty stdout; no `Traceback`; first stderr line exactly `Cannot read script <path>: No such file or directory` / `Is a directory` / `Permission denied`. Fails if run as root instead of skipping. | pass (was spec question #19) |
| P6-26 | `test_cli_yaml_syntax_error_is_clean_error` (parametrized: bad indent, unclosed quote, tab indent, two documents, undefined alias, `!!python/object` tag) | CLI | CLI, raw text | rc 1; no `Traceback`; first line exactly `YAML error in <path>, line L, column C: <problem>` (1-based, from `problem_mark`); the only other line is the indented YAML context, e.g. `  while scanning a quoted scalar (line 3, column 10)` | pass (was spec question #19) |
| P6-27 | `test_cli_undecodable_script_is_clean_error` (parametrized: `\xff\xfe`, truncated `\xc3`, `\x07`) | CLI | CLI, raw bytes | rc 1; no `Traceback`; first line exactly `YAML error in <path>, position N: <reason> (utf-8 byte #xff)` / `(character #x07)` | pass (was spec question #19) |
| P6-28 | `test_cli_utf16_with_bom_loads` | CLI | CLI, F1 script encoded UTF-16 with BOM | rc 0; `got-2-hi` in stdout | pass |
| P6-29 | `test_cli_errors_print_markup_like_text_verbatim` | CLI | CLI | `--arg '[/x]'`, validation input `"[/x]"` and a missing path containing `[/d]` and `[b]` are printed verbatim; no rich `MarkupError` | pass |
| P6-30 | `test_cli_load_does_not_swallow_keyboard_interrupt` | CLI | in-process, `yaml.load` patched to raise `KeyboardInterrupt` | `cli._load` re-raises `KeyboardInterrupt` | pass |
| P6-31 | `test_cli_script_error_at_runner_load_is_clean_error` (parametrized: `env: {A: "{{ nope( }}"}`, top-level prompt `send: "{{ x "`, env self cycle `{A: "{{ env.A }}x"}`, mutual cycle `{A: "{{ env.B }}", B: "{{ env.A }}"}`, 3-cycle `A -> B -> C -> A`) | CLI | CLI; `prepare` and `spawn` each touch a marker file | rc 1; no `Traceback`; first line exactly `Script error in <path>: template error: ...` / `env cycle: A -> A` / `env cycle: A -> B -> A` / `env cycle: A -> B -> C -> A`; neither marker exists | pass |
| P6-32 | `test_cli_runtime_errors_are_not_caught_as_load_errors` | CLI | CLI, F1, `cmd: "echo {{ nope( }}"` | rc 1; `>> attach:` logged; no `Script error`; the run's `ValueError: template error: ` still reaches stderr (only building the `Runner` is guarded) | pass |
| P6-33 | `test_cli_duplicate_key_is_clean_error` (parametrized: second top-level `script:`, second `cmd:` in a step, second key in `env`) | CLI | CLI, raw text; `prepare` and `spawn` each touch a marker file | rc 1; no `Traceback`; first line exactly `YAML error in <path>, line L, column C: found duplicate key '<key>'` at the repeated key; the only other line is `  first defined (line L, column C)` at the first one; neither marker exists | pass |
| P6-34 | `test_duplicate_key_rejected_at_any_level` (parametrized: `attach`, prompt, `fn` step, nested `vars`, inside a `<<` merge source, two `<<` keys, `x` and `"x"`) | CLI | in-process, `yaml.load(..., Loader=cli.UniqueKeyLoader)` | `ConstructorError`; `problem` is `found duplicate key '<key>'` on the expected line; `context` is `first defined` | pass |
| P6-35 | `test_cli_merge_key_override_is_accepted` | CLI | CLI, F1; `vars.over: {<<: *base, b: 3}` with `base: {a: 1, b: 2}` | rc 0; `over-1-3` in stdout | pass |
| P6-36 | `test_merge_keys_load_like_safe_load` (parametrized: override, `<<: [*a, *b]` plus override, a node merged before it's aliased, a plain `=` key) | CLI | in-process | `UniqueKeyLoader` result equals `yaml.safe_load` | pass |
| P6-37 | `test_cli_keys_that_only_look_alike_are_accepted` | CLI | CLI, F1; `vars.m: {1: int, "1": str}` | rc 0; `keys-2-int-str` in stdout | pass |
| P6-38 | `test_cli_send_each_error_is_clean_error` (parametrized: missing key, missing nested key, missing middle key, path through a string, path through a list, a string / number / null / mapping instead of a list, item missing a field, item not a mapping, null field, mapping item without `fields`) | CLI, `sendEach` | CLI; top-level prompt `sendEach`; `prepare` and `spawn` each touch a marker file | rc 1; no `Traceback`; first line exactly `Script error in <path>: prompt 'login': sendEach '<each>': <problem>`; neither marker exists | pass |
| P6-39 | `test_cli_send_each_path_outside_vars_is_validation_error` (parametrized: `env.HOME`, `args.pw`, `session.before`, `creds`, `vars`, `vars..creds`, `vars.creds.`) | CLI, `sendEach` | as P6-38 | rc 1; first line `Validation errors:`; stderr has `each must be a path under vars, like vars.creds, got: <each>`; neither marker exists | pass |
| P6-40 | `test_cli_valid_send_each_runs` (parametrized: nested path with `fields` entries; string/number/boolean items without `fields`; strings without `fields`) | CLI, `sendEach` | CLI, F1, top-level prompt `sendEach` that never fires | rc 0; `got-2-hi` in stdout | pass |
| P6-41 | `test_null_corpus_covers_every_model`, `test_parity_null_optional_field` (parametrized over every optional field of every model, generated from `model_fields`) | YAML Script Structure | F7; `HOSTS` places each model in a minimal document | `<key>: null` → `(False, False)`; one model error at the key; for a `None`-default field its type is `null_value` with `null (an empty value) is not allowed; omit the key instead`. The coverage test fails if a model with optional fields (other than `PluginStep`) has no host, so new models and fields get cases automatically. | pass |
| P6-42 | `test_omitted_optional_field_gets_default` (parametrized as P6-41) | YAML Script Structure | F7 | the host document without the key → `(True, True)`; the parsed field equals its default. The key is built as `OMIT` and dropped, because a prompt's host needs either `expect` or a `fields` `sendEach`, and must leave out whichever key is tested. | pass |
| P6-43 | `test_null_plugin_common_prop` (parametrized: `after`, `delay_after`, `delay_before`, `timeout`, `when`) | Common Step Properties | model, F6 `probe` | `{probe: x, <prop>: null}` → one `null_value` error at `script.0.plugin.<prop>`. Model only: the static `pluginStep` accepts any object. | pass |
| P6-44 | `test_parity_null_sleep` | Duration Format | F7 | `sleep: null` → `(False, False)`; model message exactly `Value error, invalid duration: null` at `script.0.sleep.sleep` (it used to sleep 0) | pass |
| P6-45 | `test_cli_empty_value_is_validation_error` (parametrized: step `after:`, `attach.env:`, `timeout: ~` on a `line`, `sleep:`) | YAML Script Structure, CLI | CLI, raw text; `prepare` and `spawn` each touch a marker file | rc 1; first line `Validation errors:`; the single JSON error has the expected `loc` and message (`null (an empty value)`, `Extra inputs`, `invalid duration: null`); neither marker exists | pass |

| P6-46 | `test_cli_old_fields_list_is_validation_error_with_hint` | CLI, Migrating from 2026-08 | CLI; `send: {each: vars.pairs, fields: [username, password]}` with a grouped `expect`; `prepare` and `spawn` each touch a marker file | rc 1; first line `Validation errors:`; two `fields_entry` errors at `prompts.1.send.fields.0` and `.1`, the second exactly `since 2026-10 a fields entry pairs a regex with a field: write {match: <regex>, field: password} (see "Migrating from 2026-08" in SPEC.md)`; neither marker exists | pass |
| P6-47 | `test_cli_old_version_is_validation_error_with_hint` | CLI, Top-level fields | as P6-46 with `autobot: 2026-08` | rc 1; `Validation errors:`; one `unsupported_version` error at `autobot` with the migration hint; neither marker exists | pass |
| P6-48 | `test_send_each_fields_accepted`, `test_send_each_fields_rejected` (parametrized) | `sendEach` | model | accepted: string / list `match`, two entries, the same field twice, a dotted field (a plain key), `sendEach` without `fields` over regex `expect`, block prompt with `fields`. Rejected, each with its exact location and type (and message where autobot writes it): `fields: []` (`too_short`), `match: []` (`too_short`, `match must be a regex or a non-empty list of regexes`), a non-string or mapping `match`, missing `field` / `match`, non-string `field`, an extra key, the old list form (`fields_entry` per entry), `expect` next to `fields` (also `expect: []`) and in a block (`script.0.block.block.prompts.0.expect`, `expect_with_fields`), no `expect` without `fields` (`missing`, three ways), a grouped `expect` with `sendEach` without `fields` (`grouped_expect` at `prompts.0.expect.1`, the message P6-52 gives every prompt), a bad `each` path | pass |
| P6-49 | `test_parity_send_each_accepted`, `test_parity_send_each_rejected` (parametrized over the P6-48 corpora) | `sendEach` | F7 | accepted → `(True, True)`; rejected → `(False, False)`: every `sendEach` rule is in the schema, none is model-only | pass |
| P6-50 | `test_return_without_send_accepted`, `test_return_with_send_rejected` (parametrized: a string, `send: ''`, `sendEach`, `sendEach` with `fields` and no `expect`, block prompt), `test_return_with_send_reported_with_other_prompt_errors`, `test_invalid_send_reported_on_its_own` | prompts | model | `return: true` without `send`, and `send` with `return: false` (string, `fields`), accepted. Each `return: true` + `send` form → exactly one `return_with_send` error at `...send` with `a return prompt is a shell prompt and sends nothing; remove send or return` (block: `script.0.block.block.prompts.0.send`). With `fields` and `expect` → `return_with_send` then `expect_with_fields`; a `send` string without `expect` → `return_with_send` then `missing` at `expect`; with a grouped `expect` → `return_with_send` then `grouped_expect`. A `send` list or boolean on a return prompt → only `send_list` / `send_type` | pass |
| P6-51 | `test_parity_return_without_send_accepted`, `test_parity_return_with_send_rejected` (parametrized over the P6-50 corpora) | prompts | F7 | accepted → `(True, True)`; rejected → `(False, False)`, including the `fields` prompt with no `expect` (the schema's `allOf` checks the `return` rule next to the `expect` rules) | pass |
| P6-52 | `test_simple_prompt_accepted`, `test_expect_string_is_a_one_item_list`, `test_removed_prompt_forms_rejected` (parametrized) | prompts, Migrating from 2026-08 | model | accepted: `expect` alternatives with `send: 'yes'`, `expect` as a string (stored as a one-item list), `send: ''`, a template, a shell prompt with a string `expect`, a block prompt. Rejected, each exactly one error with location, type and message: a flat, one-item, list-of-lists, mixed or empty `send` list (`send_list` at `prompts.0.send`), a boolean, integer or float `send` (`send_type`), a grouped or mixed `expect` with a string `send`, with no `send`, and on a return prompt (`grouped_expect` at `prompts.0.expect.M`), a list `send` in a block prompt (`script.0.block.block.prompts.0.send`) | pass |
| P6-53 | `test_parity_simple_prompt_accepted`, `test_parity_removed_prompt_forms_rejected` (parametrized over the P6-52 corpora) | prompts | F7 | accepted → `(True, True)`; rejected → `(False, False)`: none of the new rules is model-only | pass |
| P6-54 | `test_cli_quoted_send_yes_is_sent` | prompts | CLI; F2 spawn `--order none --ask continue? --ask 'are you sure?'`; the YAML text has `send: 'yes'` | rc 0; device log exactly `ASK=yes, ASK=yes`; the script's `cmd` output follows | pass |
| P6-55 | `test_cli_unquoted_send_scalar_is_validation_error_with_hint` (parametrized: `yes`, `no`, `on`, `true`, `1234`) | prompts | as P6-54 with the YAML text `send: <value>` unquoted | rc 1; `Validation errors:`; one `send_type` error at `prompts.1.send` with the quoting hint; the device never ran | pass |
| P6-56 | `test_cli_removed_prompt_forms_are_validation_errors_with_hint` (parametrized: flat `send`, list-of-lists `send`, grouped `expect`) | CLI, Migrating from 2026-08 | CLI; `prepare` and `spawn` each touch a marker file | rc 1; `Validation errors:`; one `send_list` / `grouped_expect` error whose message names `sendEach with fields` and the migration section; neither marker exists | pass |
| P6-57 | `test_empty_expect_or_match_rejected` (parametrized), `test_whitespace_regex_accepted` | prompts, `sendEach`, Migrating from 2026-08 | model | the exact error list (location, type, message) for: `expect: []` with a `send` string, with no `send`, with a `sendEach` without `fields`, and in a block (`script.0.block.block.prompts.0.expect`), each one `too_short` at `...expect` (`expect must be a regex or a non-empty list of regexes`); `expect: ''` → `string_too_short` at `prompts.0.expect`, an entry `''` → at `prompts.0.expect.M` (`a regex must not be empty: an empty regex matches at once, before any output`); an empty entry and a grouped entry → both errors, in entry order; `return: true` + `send` with `expect: []` or `''` → `return_with_send` then the empty-`expect` error; `expect: []`, `''` or `['']` next to `fields` → only `expect_with_fields`; `match: ''` → `string_too_short` at `...fields.K.match`, a `match` entry `''` at `...match.M` (also in a block). `expect: ' '` and `match: [' ']` accepted | pass |
| P6-58 | `test_parity_empty_expect_or_match_rejected` (parametrized over the P6-57 corpus), `test_parity_whitespace_regex_accepted` | prompts, `sendEach` | F7 | rejected → `(False, False)` (the schema's `$defs.regexes`: `minItems: 1`, `minLength: 1`); a single-space regex → `(True, True)` | pass |
| P6-59 | `test_cli_empty_expect_or_match_is_validation_error` (parametrized: `expect: []`, `expect: ''` on a return prompt, a `match` entry `''`) | CLI, prompts, `sendEach` | CLI, raw text; `prepare` and `spawn` each touch a marker file | rc 1; `Validation errors:`; exactly one error with the location, type and message of P6-57; neither marker exists | pass |

Totals: 54 pass.

P6-13 and P6-14 were xfail #11 (SPEC.md:37 allows mixed entries, SPEC.md:24 requires `YYYY-MM`). Both pass now that the schema is normative and the model and schema were fixed.

New parity case from #12: a `call` to an undefined function is `(False, True)`: the model rejects it (`undefined_function`) and the static schema accepts it. Keep it out of P6-12's reject corpus; P7-08 covers it.

## P7: registry and plugins

Files: `tests/test_registry.py` (new), `tests/test_plugins.py` (extend) and `tests/test_schema_plugins.py`. SPEC.md:286; charter (plugins through `autobot.steps` entry points).

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P7-01 | `test_builtins_registered_and_not_plugins` | 87-270 | F6 | `keys()` ⊇ the 7 builtin keys; `plugin_executors()` contains none of them | pass |
| P7-02 | `test_builtin_only_config_does_not_discover` | 286 | F6 (call counter) | validating a config with only builtin steps → `discover_calls == 0` | pass |
| P7-03 | `test_unknown_key_raises` | 286 | F6 | `reg.get("nope")` raises `ValueError` `unknown step type: nope`; discovery ran exactly once | pass |
| P7-04 | `test_plugin_model_receives_only_plugin_fields` | 286 | F1, F6 `register_plugin` with an `extra="forbid"` model | step with `after`/`when`/`delay_before`/`delay_after`/`timeout` runs; executor receives an instance of its own model with no common-prop attributes; `timeout` arrives as the argument | pass |
| P7-05 | `test_validate_plugin_step_requires_key` | 286 | F6 | `PluginStep` without `plugin_key_` → `ValueError` `no plugin key` | pass |
| P7-06 | `test_plugin_step_error_propagates` | 286 | F1, F6 executor raising `RuntimeError("boom")` | `Runner.run()` raises `boom`; session closed (`children`) | pass |
| P7-07 | `test_schema_command_includes_plugin_defs` | 12 (and #16) | CLI subprocess `schema` with plugin on `PYTHONPATH` (F6 `plugin_dist`) | output JSON has `$defs.echoStep`; `$defs.step.oneOf[-2] == {"$ref": "#/$defs/echoStep"}`; `oneOf[-1]` is still `pluginStep`; `pluginStep.not.anyOf` ends with `{required: [echo]}`; the output accepts `{echo: hi, timeout: 5s}` and rejects `{echo: 1}` | pass |
| P7-08 | `test_plugin_field_typo_fails_at_load` / `test_call_undefined_fn_fails_at_load` (2 tests) | 12, 180 | F6 plugin; `{echo: hi, ech0: x}` / `call: nope` | `ValidationError` at `Config` time: `extra_forbidden` at `("script", 0, "ech0")` / `undefined_function` at `("script", i, "call")`, message `call to undefined function 'nope'`. For both, the CLI exits 1 with `Validation errors`, no `Traceback`, and neither `prepare` nor spawn runs (check a `prepare` marker file) | pass (was todo #12) (x2) |
| P7-09 | `test_generated_schema_parity_accept` / `_reject` (parametrized corpus) | CLI, Common Step Properties | F6 `probe` plus in-process `nest` (nested model, optional fields) and `free` (`extra="allow"`) plugins; F7; schema built in-process with `cli.add_plugin_steps` | accept for both model and generated schema: `{probe: x}`, with each common prop and all of them, inside a block and a `fn`, a nested model, `null` for the plugin's own optional fields, extra keys and a common prop on the `extra="allow"` plugin. Reject for both: `null` and wrong-typed values of each common prop, an unknown extra key, wrong plugin field types (top level and nested), two plugin keys in one step, a plugin key next to `cmd`, a script-set `plugin_key_` | pass |
| P7-10 | `test_unknown_step_key_falls_to_plugin_step` (parametrized) | YAML Script Structure, CLI | as P7-09 | `{nope: 1}`, `{nope: 1, timeout: 5s}`, `{timeout: 5}` → model rejects, generated and static schema accept via `pluginStep` (the static-schema divergence SPEC allows) | pass |
| P7-11 | `test_generated_schema_shape` / `test_step_common_matches_builtins_and_model` | CLI, Common Step Properties | as P7-09 | the generated schema is valid 2020-12; plugin refs sit before `pluginStep` in registration order; `pluginStep` excludes each plugin key; each `<key>Step` starts with `$ref stepCommon`; nested model defs live under `<key>Step.$defs`. `stepCommon` has exactly `PluginStep`'s common keys and equals `cmdStep`'s definitions of them | pass |
| P7-12 | `test_validation_leaves_input_unchanged` / `test_internal_plugin_key_rejected` | YAML Script Structure | as P7-09 | `Config.model_validate(doc)` leaves a document with plugin steps (in a block and a `fn`) deep-equal to its copy, and the parsed step still has `plugin_key_`; `{probe: x, plugin_key_: nest}` → one `extra_forbidden` error at `("script", 0, "plugin")` | pass |

Totals: 13 pass.

P7-09..12 were added with the fix for the generated schema found in the PR #23 review: a plain plugin step matched both `<key>Step` and `pluginStep`, so `oneOf` rejected it, and a plugin step with a common prop or an invalid field fell through to `pluginStep` and was accepted unchecked. P7-12 came with it: `PluginStep`'s before-validator wrote `plugin_key_` into the caller's dict, and accepted (then overwrote) a `plugin_key_` set in the script, which the schema rejects. It now works on a copy and rejects a script-set `plugin_key_`.

P7-07 pins the `schema` subcommand. The subcommand is clearly intentional (help text, plugin merging), but SPEC and README don't document it (#16). If the user decides to remove or rename it, drop or adjust this test.

## P8: low priority

Files: `tests/test_types.py` (new), `tests/test_output_capture.py` (extend the strip_echo table), `tests/test_simple_steps.py` (new).

| ID | Test | SPEC | Setup | Assertion | Status |
|----|------|------|-------|-----------|--------|
| P8-01 | `test_parse_duration` (parametrized) | 307-313 | unit | `5`→5.0, `5.5`, `"5s"`, `"500ms"`→0.5, `"2m"`→120, `"1h"`→3600, `"1.5s"`; `"5"`, `"5x"`, `""`, `"ms"`, `"-1s"`, `"1.5.5s"`, `None` → `ValueError`; `None` reports `invalid duration: null` (it was 0 until P6-44) | pass |
| P8-02 | `test_ensure_list` | n/a (helper) | unit | `None`→`[]`, `"a"`→`["a"]`, `["a"]`→`["a"]` | pass |
| P8-03 | `test_render_passthrough_and_filters` | 315-334 | unit | non-string returned unchanged; `contains`/`search` filters registered | pass |
| P8-04 | `test_render_trailing_newline_dropped` | 292 | unit | `render("x\n", {}) == "x"` (this is what makes P3-03's `"false\n"` falsy) | pass |
| P8-05 | `test_strip_echo` (extend table) | 112 | unit | new rows: echo wrapped mid-word `("echo ab\ncd\nout\n", "echo abcd")` → `"out\n"`; mismatched echo unchanged; partial echo prefix only unchanged; scroll form with non-matching tail unchanged; output identical to the command on the next line keeps the second copy; whitespace-only `sent` unchanged | pass |
| P8-06 | `test_control_sends_in_order` | 265-270 | F1, F4 | `control: [a, x]` → `("ctrl","a"), ("ctrl","x")` | pass |
| P8-07 | `test_control_bracket_is_ctrl_bracket` | 268 | F2 `--rawdump 1 --wait-enter` | `control: "]"` → device log `RAW=1d` | pass |
| P8-08 | `test_return_sends_n_newlines` | 258-263 | F1, F4 | `return: 3` → three `""` lines | pass |
| P8-09 | `test_line_list_sends_each_without_waiting` | 252-256 | F1, F5 | `line: ["echo a", "echo b"]` → two `sendline`s, no `get_prompt` in the timeline for the step | pass |
| P8-10 | `test_line_and_after_are_rendered` | README "Templating" | F1, F4, `vars: {x: hi, pat: MARK}` | `line: "echo {{ vars.x }}"` sends `echo hi`; `after: "{{ vars.pat }}"` waits for `MARK` | pass (decision #9: stays templated) |
| P8-11 | `test_sleep_step` | 174-178 | F5, then F1 real sleep | `sleep: 2s` → `sleep(2.0)` recorded; real `sleep: 500ms` elapses 0.5 to 2 s | pass |
| P8-12 | `test_call_runs_function_steps` | 55-66, 180-184 | F1, `fn: {f: {script: [cmd: echo a (register a), cmd: echo b (register b)]}}` | `vars.a == "a"`, `vars.b == "b"` | pass |
| P8-13 | `test_call_steps_see_registered_vars` | 322 | F1 | `cmd: echo v` `register: v`; `fn` step `echo got-{{ vars.v }}` registers `got-v` | pass |
| P8-14 | `test_attach_env_empty_dict_is_not_omitted` | 75 | F4 `spawned` in raise-after-record mode | `attach.env: {}` → spawn kwargs `env == {}` (not the default) | pass (was xfail #15) |
| P8-15 | `test_log_lines_print_markup_like_text_verbatim` (parametrized: `[/x]`, `[bold]x[/bold]`) | n/a (operator log) | CLI subprocess, F1; the text is in the spawn line (`env X=<text> bash ...`), a block name and `cmd: echo "<text>"` | rc 0; no `Traceback`/`MarkupError`; stderr has the lines `>> attach: env X=<text> ...`, `>> block enter: <text>`, `>> cmd: echo "<text>"`, `>> block completed: <text>` verbatim | pass (finding #20) |
| P8-16 | `test_log_lines_are_not_wrapped` | n/a (operator log) | CLI subprocess, F1, a 40-word `echo` | the whole `>> cmd: echo word0 ... word39` is one stderr line | pass (finding #20) |

Totals: 16 pass.

## Implementation status

All rows above are implemented. The original `pass` and `xfail` rows landed on branch `test/impl-p1-p8`; the 14 former `todo` rows (decisions #4, #6, #9, #10, #11, #12) landed on branch `test/spec-decision-tests` and all pass (see [Decision tests](#decision-tests)). Every xfail uses `xfail(strict=True, raises=..., reason="finding #N")`. Each one was run with `--runxfail` to confirm that it fails for the stated reason.

| Priority | pass | xfail | slow | Files |
|----------|-----:|------:|-----:|-------|
| P1 | 22 | 0 | 1 | `test_cmd_semantics.py` |
| P2 | 15 | 0 | 0 | `test_cmd_forms.py`, `test_embedded_script.py` |
| P3 | 18 | 0 | 0 | `test_common_props.py` |
| P4 | 23 | 0 | 3 | `test_get_prompt.py` |
| P5 | 32 | 0 | 0 | `test_lifecycle.py`, `test_env.py` |
| P6 | 37 | 0 | 0 | `test_models.py`, `test_schema_parity.py`, `test_examples.py`, `test_cli.py` |
| P7 | 13 | 0 | 0 | `test_registry.py`, `test_plugins.py`, `test_schema_plugins.py` |
| P8 | 16 | 0 | 0 | `test_types.py`, `test_output_capture.py`, `test_simple_steps.py` |
| **Total** | **176** | **0** | **5** | |

Findings #1, #2, #3, #5 and #18 are fixed (branch `fix/xfail-bugs-1-2-3-5-18`); their xfail markers are removed and the rows above say `pass (was xfail #N)`. Findings #7, #8 and #15 are fixed the same way (branch `fix/xfail-bugs-7-8-15`: P4-20, P2-06, P8-14); no xfail rows remain. Finding #19 is fixed on branch `fix/cli-load-errors-19` (P6-25..30); SPEC.md's CLI section now lists every load error, and all of them exit 1. Finding #20 is fixed on the same branch (P8-15, P8-16). The same branch also reports `ValueError`s raised while the `Runner` is built (top-level `env` and prompt `send` templates) as `Script error in <path>: ...` with rc 1, before `prepare` (P6-31, P6-32). Duplicate mapping keys are rejected at load time as a `YAML error` instead of silently keeping the last value (P6-33..37).

Decisions #4, #6, #9, #10, #11 and #12 landed on branch `feat/spec-decisions-4-6-9-10-11-12`. That branch also:
- removes the P6-13/P6-14 xfail markers (both pass)
- changes the expected breakout log in P5-09/P5-16 to `(StepFailure)`, because rc failures are now `StepFailure` (#10)
- adds `fn: {f: {script: []}}` to P6-06, P6-11[every-step] and P3-13[call], because a `call` to an undefined function is now a load-time error (#12)
- adds one test outside the plan: `test_plugins.py::test_plugin_error_with_braces_survives_load_time_check` (#12)

SPEC.md line numbers cited in this plan refer to SPEC.md before that branch. Several sections moved.

Test functions are named `test_pN_MM_*` after their plan ID. P8-05 adds rows to the existing `test_strip_echo` table.

Runtime on the reference machine: full suite about 100 s (311 passed, 5 xfailed, with `jsonschema` installed), `-m "not slow"` about 82 s at the time of the P1-P8 implementation. The original 55 tests went from about 70 s to about 25 s once the `run()` helpers were folded into F1. The added tests missed the < 70 s non-slow target. The remaining cost is per spawn: pexpect waits 50 ms before every send and about 0.1 s when it closes a child, and there are about 180 spawning tests. `pytest-xdist` would be the next lever. It isn't added here.

Current runtime (2026-10-02, 842 tests, 0 skipped, `jsonschema` installed): full suite about 157 s. The closed-connection message tests (P3-18, P4-36, P5-49..51) took the count from 832 to 842; each spawns a shell or a short-lived process that exits at once (P5-50 waits 0.5 s). Before them (832 tests): about 154 s. The empty-`expect` tests (P6-57..59) took the count from 795 to 832; they are in-process model and schema checks, apart from three CLI subprocesses, and add no measurable time. Before them (795 tests): about 154 s. The timeout-message tests (P4-35, P5-46..48) took the count from 789 to 795; each spawns a shell or `sleep` and waits out a deadline of at most 1 s. Before them (789 tests): about 148 s. The `prepare` shebang fallback (P5-04, P5-42..45) took the count from 776 to 789: 14 tests replace the one P5-04 test, each runs a short local script, and those that succeed spawn a shell. Before it (2026-10-01, 776 tests): about 147 s. The block prompt-swap fix (#14; P4-32..34, P5-35..41) took the count from 749 to 776. The 27 new tests add about 9 s: 13 are in-process checks, the rest spawn a shell; with the fix, none of them waits the 5 s idle poll (P4-33 `other`, P5-39 and P5-40 each wait out a 1 s deadline). The 749 earlier tests took about 138 s with the fix and about 143 s without it. Only `test_block_breakout_after_timeout_restores_handlers` got clearly faster (6.4 s to 1.4 s); the other block tests reach their blocks with no prompt matched yet, or with a `line`, so the swap never cost them a wait. Before the fix (749 tests): about 143 s. The spawn-failure tests (P5-32..34) took the count from 742 to 749: eight added, and `test_attach_initial_timeout_closes_child` folded into P5-32. They spawn short-lived children and four CLI subprocesses. Earlier (735 tests): about 124 s. The generated-schema fix and the `PluginStep` copy fix (P7-09..12) took the count from 696 to 735; their tests are in-process schema and model checks. Before it (2026-09-30, 696 tests): about 143 s. The simple-`send` change (P4-26..31, P6-52..56, and the migrated and removed rows) took the count from 649 to 696. Its new tests are in-process model and schema checks, apart from seven fake-device tests and nine CLI subprocesses. Before it (649 tests): about 122 s. The 19 tests of P6-50/51 (`return` with `send`) are in-process model and schema checks. The 59 tests added with the `sendEach` fields entries (P4-24/25, P6-46..49 and the migrated rows) are in-process model and schema checks, apart from two fake-device tests and two CLI subprocesses. Before them (571 tests): about 121 s. The 122 tests added with P6-41..45 (including the P8-01 null message) are in-process model and schema checks, apart from four CLI subprocesses. Before them (2026-09-29, 449 tests): full suite about 137 s; `-m "not slow"` about 109 s (445 tests); the four `slow` tests about 29 s. Slow-marker timings: P4-05 11.1 s, P1-18 6.3 s, P4-06 6.2 s, P4-04 5.2 s (each waits on the 5 s idle poll); P4-18 0.2 s, so it is no longer marked.

### Deviations from the plan

Fixtures:
- F1: `make_doc()` returns the raw dict, which the CLI tests need. `make_config`, `make_runner`, `run_script` and `run_vars` (returns `config.vars`) build on it. The default `timeout: 5s` is only added to `cmd`/`call`/`block`/`control` steps, not to plugin steps, so the existing plugin-timeout assertions don't change. `attached_runner(**make_config_kwargs)` is a factory.
- F3: retired. `FakeSession`/`FakeCtx` were removed from `conftest.py` because no test used them. Their only planned users were the P1-20 (#10) tests, which ended up using the real shell for every case (see [Decision tests](#decision-tests)).
- F2: the unused `--banner` and `--max-attempts` device options were removed; the banner is always `Welcome`.
- F4/F5: stop modes are `spawned.stop = True` (raises `SpawnRecorded`) and `timeline.stop_attach = True` (raises `AttachRecorded`). Timeline events are `(name, principal_arg)`; full calls are in `timeline.calls`.
- F6: `autobot/__init__.py` re-exports the `registry` instance, which shadows the `autobot.registry` submodule attribute. The fixtures therefore patch the modules taken from `importlib.import_module`. `plugin_dist(root, key, source, target)` takes the entry-point target explicitly. `ProbeExecutor` also records a snapshot of `session.ctx`.

Tests:
- P1-09: the pattern is `^ERR.*`, so the message is `command error: ERR x`. `^ERR` alone would produce `command error: ERR`.
- P1-18: expects `abc\ndef`, not `abcdef`. The solicit newline's tty echo is printed between the two parts, and SPEC.md:111 defines captured output as what the session prints. Before the #1 fix the value was `abcabc\ndef`.
- P1-19: asserts `session.before == ""` right after `true`, so the failure points at the cause (stale `MARKX\n`) rather than the consequence.
- P2-04, P3-01: the `after` marker is assembled by `printf`, so the echoed command can't match it.
- P3-09: uses the F6 probe to snapshot `session.before`/`match` during the step, and `printf '%sion: V%s\n' Vers 42`, so neither `Version` nor `V\d+` appears in the echo.
- P4-01: sends `echo hi` instead of `""` (written while #5 left `session.ctx` stale on empty output). It first calls `get_prompt()` to reach bash's first prompt, which is no longer swallowed at attach (#3), then clears the send log.
- P4-02, P4-04, P4-19: use `--order none`. The fake-device tests that kick (P4-06, P4-15 and others) use `--wait-enter`, where the device blocks until it reads that Enter; the kick is part of the device protocol, not a #3 workaround, and stays after the #3 fix. P4-15 uses `--then prompt`.
- P4-19: `get_prompt(timeout=3)` must return; before the #3 fix it raised `TimeoutError` (converted to an assertion). The test doesn't time an elapsed < 4 s.
- P4-20: the `ValueError` from building the `Runner` is re-raised as `AssertionError`, so the xfail was pinned to #7. The re-raise stays now that #7 is fixed: a regression fails with that message. The #7 fix adds one test outside the plan, `test_send_template_syntax_error_at_load`: a malformed `send` still fails when the `Runner` is built, as `ValueError("template error: ...")`. Wrapping every template error adds two parametrized tests outside the plan in `test_types.py`: `test_every_template_error_is_a_value_error` and `test_check_template_reports_syntax_errors_like_render`.
- P5-10: `enter` uses `line: "PS1='BL''K$ '"`. A `cmd` would first wait for the new prompt, which isn't printed yet. The quoting keeps the echo from matching `BLK\$ `.
- P5-11: no kick is needed; bash's first prompt, still pending after `attached_runner` attaches, serves the first `get_prompt`.
- P6-07, P6-09, P8-01 and P8-11 are split into two functions each (accept/reject, recorded/real). They still count as one plan row.
- P6-14: asserts only the model side. Python's `jsonschema` evaluates `pattern` with `re.search`, where `$` also matches before a trailing newline, so the Python validator accepts `"2026-08\n"` too. An ECMA-262 validator would reject it.
- P8-07: synchronizes on the device's `RAW> ` / `RAW=<hex>` markers with `after`. It passes `attach_script=[{"return": 1}]` explicitly: its `--wait-enter` device needs that Enter, which it used to get from the F1 default.

No new product findings came up during implementation. #18 (P6-24) reproduced as planned (`TypeError: ... argument after ** must be a mapping` with a traceback) and is now fixed.

### Decision tests

The 14 former `todo` rows are written (branch `test/spec-decision-tests`) and all pass on `main` at c9e09bd; no new findings. Full suite: 363 passed, 3 xfailed, 0 skipped, about 107 s (three identical runs).

| Row | Decision | Tests (file) | Collected items |
|-----|----------|--------------|----------------:|
| P1-20 | #10 | `test_p1_20_ignore_error_does_not_swallow_{timeout,eof,template_error,invalid_regex,responses_exhausted}` (`test_cmd_semantics.py`) | 5 |
| P3-05 | #6 | `test_p3_05_when_none_value`, `test_p3_05_when_surrounding_whitespace_and_case` (`test_common_props.py`) | 11 |
| P4-17 | #4 | `test_p4_17_fields_{password_first,password_first_credential_cycling,password_only,exhaustion,login_first_exhaustion_after_two_sets}` (`test_get_prompt.py`) | 5 |
| P5-05 | #9 | `test_p5_05_prepare_is_templated`, `test_p5_05_prepare_bash_array_length_needs_raw` (`test_lifecycle.py`) | 2 |
| P6-15..18 | #11 | `test_p6_15_parity_return_minimum`, `test_p6_16_parity_negative_duration`, `test_p6_17_parity_bool_duration`, `test_p6_18_parity_fn_script_required`, `test_p6_strict_bool_{ignore_error,prompt_return}` (`test_schema_parity.py`) | 15 |
| P7-08 | #12 | `test_p7_08_plugin_field_typo_fails_at_load`, `test_p7_08_call_undefined_fn_fails_at_load`, `test_p7_08_call_undefined_fn_checked_everywhere`, `test_p7_08_recursive_call_is_accepted` (`test_plugins.py`) | 10 |

Deviations from the row targets:
- P1-20: every case uses the real shell (F1), not the retired F3. EOF comes from `cmd: exit`, which closes bash before the next prompt. The timeout case uses `sleep 30` with `timeout: 1`. "The next step doesn't run" is checked with the F6 probe as the next step. Added cases beyond the three planned: an invalid `assert` regex (`re.error`) and `responses exhausted` from a prompt inside the command (the command prints `LOG%s: ` so its echo can't match `LOGIN: `).
- P3-05: the whitespace test is parametrized over `" false "`, `"\tFALSE\n"`, `NONE`, `" None "`, `" 0 "`, whitespace only, `False`, and `no`/`off`/`" no "` (which run).
- P4-17: adds SPEC's table row 4 (login-first, two sets, a third `login:` → `responses exhausted`). The tests used grouped `expect` entries with a literal `send` until those forms were removed; they now use `sendEach` with `fields`, which has the same positional rule. The missing-item test was removed with its message.
- P5-05: follows SPEC's example `arr=(a b c)`, so the raw form writes `3`; the row said `arr=(a b)` → `2`. The templated test renders `env` (with an OS override and nesting), `vars` and `args`. The failing case asserts `ValueError` matching `^template error: ` (it asserted `jinja2.TemplateSyntaxError` until all template errors were wrapped, branch `fix/xfail-bugs-7-8-15`), no prepare output, no `_autobot_*` temp file and no `attach` call.
- P6-15..18: each also asserts the model error location; P6-18 checks that `{script: []}` is accepted by both. Strict booleans (`"yes"`, `"true"`, `1`, `0`) for `ignore_error` and prompt `return` are two extra parametrized tests (`bool_type` at the field).
- P7-08: the in-process typo test registers an in-process `echo` plugin (F6); the CLI half uses the dist-info `echo` plugin via `PYTHONPATH`. Both CLI halves use `prepare` and `spawn` commands that each touch a marker file, and assert neither exists. The call test nests the call in a block (`("script", 1, "block", "script", 0, "call")`). Added: a parametrized test that checks every location SPEC lists (top-level, `when`-skipped, `attach.script`, `attach.breakout`, `fn` body, block `enter`, block `breakout`), and a test that mutual recursion is accepted.

## Spec decisions

All six decisions are made and in SPEC.md (branch `feat/spec-decisions-4-6-9-10-11-12`). Their blocked rows were `todo`; they are now written and pass (see [Decision tests](#decision-tests)).

| # | Outcome |
|---|---------|
| #4 | Option A, positional. Pattern k of a grouped entry sends item k of the current credential set, and a single-pattern entry sends the next unsent item. The set advances when a used item is picked again, or when a single pattern finds the set fully sent. SPEC "Response selection". Grouped entries and literal `send` lists were later removed; the positional rule lives on in `sendEach` `fields` entries, and a `send` string is sent on any match, each time the prompt appears. |
| #6 | Option B, normalize. Falsy if `result.strip().lower()` is in `{"", "false", "0", "none"}`. |
| #9 | Option A. `prepare`, `line` and `after` stay templated. SPEC lists every rendered field and documents `{% raw %}`. |
| #10 | Option A, command failures only (exit code, assert, `errors` match, upload mismatch). They now raise `StepFailure`, and prompt-response failures are no longer swallowed. Timeouts, EOF and template errors abort. SPEC states the `register` rules. |
| #11 | Option A, the schema is normative. `return` ≥ 1 (strict int, required); durations ≥ 0 and not bool; `fn.script` required; version `fullmatch`; strict booleans for `ignore_error` and prompt `return`; the schema allows mixed `expect`. |
| #12 | Option B, load time. A `Config` validator checks `call` targets and plugin fields everywhere, and the CLI reports `Validation errors` with rc 1 before prepare/spawn. Recursion and cycles aren't detected. |

An explicit `null` for an optional field (`timeout: null`, a bare `after:`) and `sleep: null` were accepted by the model and rejected by the schema; the model now rejects them (P6-41..45).

Known remaining model/schema divergences (not tested; follow-ups):
- `return: 1.0`: the schema accepts it (JSON Schema treats it as an integer); the strict model rejects it.
- Python `jsonschema` evaluates `pattern` with `re.search`, so `"2026-08\n"` and `"5s\n"` pass the schema in Python (an ECMA-262 validator rejects them); the model rejects both.
- Unknown step keys (item g below): the static schema accepts them as `pluginStep`.
- `call` to an undefined function: the model rejects it, the schema accepts it (see P6).

The sections below keep the original option analysis for reference.

### #4: grouped `expect` → response mapping

SPEC.md:37 says a grouped entry is "grouped alternatives" but doesn't say how responses map. README.md:147 says the mapping is positional. The code (session.py:176-189) uses one sequential cursor per prompt.

| Option | Behavior | Password-first device |
|--------|----------|-----------------------|
| A. Positional (README) | Pattern k in a group sends response k of the current attempt; the attempt advances when the group restarts. | sends the password |
| B. Sequential (current code) | Any match of any pattern sends the next response in the flat list. | sends the username (the bug the reviewer reproduced) |
| C. Sequential, documented | Same as B; README.md:147 is corrected, and users order prompts to match. | sends the username |

Blocks: P4-17. P4-10, P4-11, P4-12, P4-16 and P4-14 use login-first devices and pass under all three options. With option A, add a positional credential-cycling test (password-first device plus `[[u, p1], [u, p2]]`).

### #6: `None` and whitespace in `when`

SPEC.md:292 lists the falsy strings `""`, `"false"`, `"False"`, `"0"`, `"none"`. Jinja renders Python `None` as `"None"`, which isn't on the list, so the step runs. Surrounding whitespace also defeats the check (runner.py:175).

| Option | Behavior |
|--------|----------|
| A. Exact list (current) | Document that `None` and `" false "` are truthy. |
| B. Normalize | Falsy if `result.strip().lower()` is in `{"", "false", "0", "none"}` (`None`, `NONE`, `FALSE` and padded values become falsy). |
| C. Expression semantics | Compile `when` as a Jinja expression and use Python truthiness. This is a breaking change: `when: "no"` would become an undefined-variable error. |

Blocks: P3-05 (2 tests). With option C, P3-03 and P3-04 also need to be rewritten.

### #9: is `attach.prepare` (and `line`, `after`) templated?

SPEC.md:317 lists templated fields: `cmd`, `assert`, `attach.spawn`, `when`, prompt `send`. The code also renders `prepare` (runner.py:134), `line` (steps.py:200) and `after` (runner.py:170). README.md "Templating" says "All string values support Jinja2 templates." Because `prepare` is rendered, a bash `${#arr[@]}` is parsed as the start of a Jinja comment (`{#`) and raises `TemplateSyntaxError`.

| Option | Behavior |
|--------|----------|
| A. Templated (current) | Add `prepare`, `line` and `after` to the SPEC.md:317 list, and document `{% raw %}` for literal `{#` / `{{`. |
| B. `prepare` raw, others templated | `prepare` is passed verbatim, like an embedded file. Add `line` and `after` to the SPEC list. |
| C. SPEC list is exhaustive | Stop rendering `prepare`, `line` and `after`. This is breaking: examples may use `line` templates. |

Blocks: P5-05 (2 tests: "is templated" passes under A; "array length literal" passes under B/C). P8-10 is planned as `pass` on README's strength and would flip under C.

### #10: scope of `ignore_error`

SPEC.md:118 says "continue on failure". The code catches only `RuntimeError` (steps.py:60, 99), which covers rc, assert, `errors` and upload mismatch. It doesn't catch `TimeoutError`, `EOFError`, or `ValueError` (template or undefined-variable errors).

| Option | Behavior |
|--------|----------|
| A. Command failures only (current) | Document that timeouts, disconnects and template errors always abort. |
| B. Command failures + timeouts | Also swallow `TimeoutError`. SPEC must then say what state the session is in: the command may still be running. For example, "the next step waits for a prompt as usual", or "Ctrl-C is sent". |
| C. Everything except script bugs | Swallow `TimeoutError` and `EOFError`; template and validation errors still abort. |

Blocks: P1-20 (3 tests: timeout, EOF, template error). The `register` contents after a swallowed timeout also need a rule (partial output or nothing).

### #11: model vs JSON schema, which is normative?

SPEC.md:12 and 18 say the models validate "against `schemas/autobot.2026-08.json`", which suggests the schema is normative. The reviewer verified these divergences:

| Item | Model | Schema | Status in this plan |
|------|-------|--------|---------------------|
| (a) mixed `expect: ["a", ["b","c"]]` | accepts | rejects (`oneOf`) | SPEC.md:37 allows it → xfail P6-13 (schema side) |
| (b) `autobot: "2026-08\n"` | accepts (`re.match` + `$`) | rejects | SPEC.md:24 → xfail P6-14 (model side) |
| (c) `return: 0` / `-2` | accepts | `minimum: 1` | decided A (P6-15, pass) |
| (d) negative / bool durations | accepts (`-1`, `True` → 1.0) | `minimum: 0`, number only | decided A (P6-16, P6-17, pass) |
| (e) `fn.<name>.script` missing | defaults to `[]` | required | decided A (P6-18, pass) |
| (f) `return` step key without value | field has a default, but the discriminator requires the key anyway | required | cosmetic; no test |
| (g) unknown step key `{cmdd: x}` | rejects (no registered plugin) | accepts via the `pluginStep` catch-all | inherent to static schemas; recommend documenting it and using `autobot schema` (P7-07) for editor validation |

Options:
- **A. Schema is normative.** The model must match the schema: add `ge=1` on `return`, `ge=0` and strict numbers on durations, and make `fn.script` required. Fix (b) in the model.
- **B. Model is normative.** Generate the schema from the models (`model_json_schema()` plus a post-processing step) and check it in. A test asserts that the checked-in file equals the generated output.
- **C. Per-item.** Decide (c) to (e) individually. The reviewer's lean: `return ≥ 1`, durations ≥ 0 and not bool, `fn.script` required. That matches the schema.

Blocks: P6-15, P6-16, P6-17, P6-18. Option B also replaces P6-11/P6-12 with one "generated == checked-in" test.

### #12: load-time vs runtime validation

These errors only surface after `prepare` and `spawn`, when the step runs: plugin fields are validated by `registry.validate_plugin_step` (runner.py:185), and `call` to an undefined function raises `undefined function` in steps.py:153-155. By then, earlier steps may already have changed the remote device.

| Option | Behavior |
|--------|----------|
| A. Runtime (current) | Document it. P7-08 then pins a runtime `ValueError`/`ValidationError`. |
| B. Load time | A `Config` model validator checks `call` targets against `fn` (including nested blocks, `attach.script` and breakouts) and validates plugin steps against the plugin model. The CLI reports them as `Validation errors` with rc 1 before `prepare`. |
| C. Pre-flight | `Runner.run()` walks the tree before `prepare`/`spawn` and raises. It isn't a pydantic error, but it still happens before any side effect. |

Blocks: P7-08 (2 tests).

## Other spec questions (not tested until answered)

These are not in the list of blocking decisions. None of them is tested in this plan.

| # | Question | Source |
|---|----------|--------|
| #13 | Does `timeout` on `call`/`block` bound the nested steps, or only `after`? Today nested steps keep the 300 s default. | steps.py:152-191 |
| #14 | Answered and fixed on branch `fix/block-prompt-swap-wait`: prompt state survives a swap or restore only if the new prompts take the prompt text the last wait matched for a shell prompt (SPEC block, "Prompt state across a swap"). A swap never starts the state. Before, every swap reset it: about 5 s and a solicit newline on entry and again on exit. Covered by P4-32..34 and P5-35..41. | session.py `_set_handlers`, `_is_shell_prompt` |
| #16 | Should SPEC/README document the `run` and `schema` subcommands and `-a`? P7-07 pins `schema` in the meantime. | cli.py:65-83 |
| #17 | The solicit newline can reach a running command's stdin (SPEC-mandated). Is a note in SPEC enough? | session.py:170-172 |
| Q-a | When an embedded script times out while running, cleanup can't reach a prompt and the files stay behind. SPEC.md:171 says they are "always removed". Should cleanup interrupt the script (Ctrl-C) first? | steps.py:105-106 |
| Q-b | An undefined template variable raises `template error: ...` (StrictUndefined). Should this be in SPEC? | types.py:12, 37-38 |
| Q-c | README.md:395 says `env.*` is "merged with OS env vars". The code only overrides keys that are declared in YAML `env`, so `{{ env.HOME }}` is undefined unless declared. Which is intended? | runner.py:64 |
| Q-d | `attach.timeout: 0` falls back to 300 s. Is `0` "no wait" or "default"? | runner.py:130 |
| Q-e | Should the breakout run when the initial spawn wait fails? Today it doesn't. SPEC orders it after the main script. | runner.py:137-155 |
| Q-f | ANSI escape sequences are consumed by the prompt engine and never reach captured output. Should SPEC state it? | session.py:103 |

## New findings from planning

- **#18** (cli.py:24): an empty YAML file or a top-level list reaches `Config(**config_dict)` and crashes with `TypeError: ... argument after ** must be a mapping` and a traceback. The schema requires a top-level object, so this is a validation failure and should be reported the way other validation errors are. It's covered by P6-24. Fixed: the CLI now uses `Config.model_validate`, and SPEC.md's CLI section documents the error report.
- **#19** (cli.py:20-21): a missing file or a YAML syntax error prints a traceback. It was a spec question. Fixed: the CLI reads the file in binary mode (PyYAML decodes UTF-8, or UTF-16 with a BOM) and reports `Cannot read script <path>: <reason>` or `YAML error in <path>, line L, column C: <problem>` with rc 1, before anything runs. The CLI console also stopped interpreting rich markup, which crashed on `--arg '[/x]'` and on validation input containing `[/x]`. Covered by P6-25..30.
- **#20** (runner.py:17, steps.py:26): the operator log consoles interpreted rich markup in the text they print. `cmd: echo "[/x]"` crashed the run with `rich.errors.MarkupError` when `>> cmd:` was logged, `[bold]x[/bold]` was logged as `x`, and the same applied to block names, the spawn line and error messages. Lines longer than 80 columns were also hard-wrapped when stderr wasn't a terminal. Fixed: both consoles use `markup=False, soft_wrap=True`. Covered by P8-15 and P8-16.
- Follow-ups found while fixing #19 (not tested):
  - A `sendEach` whose `each` path doesn't exist (`each: vars.nope`) raises a bare `KeyError: 'nope'` with a traceback while the `Runner` is built. The CLI only catches `ValueError` there, so this still prints a traceback (before `prepare`).
  - Mutually referencing `env` values (`A: "{{ env.B }}"`, `B: "{{ env.A }}"`) converge to the literal `{{ env.A }}` without an error, so the run starts with unresolved values. Fixed on branch `fix/env-reference-cycle`: each default is rendered once, on first read, OS values are used verbatim, and a cycle is `env cycle: A -> B -> A` (P5-22, P5-27..31, P6-31).
  - Duplicate YAML keys are accepted and the last one wins (PyYAML). SPEC doesn't say whether they should be rejected.

## Implementation order

1. F1 to F7 in `conftest.py` and `tests/fakes/device.py`; migrate the existing duplicated helpers; add the `slow` marker, `jsonschema` and `pytest-cov`; run the existing 55 tests twice and confirm they still pass.
2. P1 and P2 (highest value, real shell only).
3. P4 (needs F2), then P5.
4. P3, P6, P7, P8.
5. Write the `todo` rows (all six decisions are in SPEC.md). Done.

Speed target: the non-slow suite should run in under the current ~70 s after the helpers stop paying the solicit wait. The four slow tests add about 29 s.
