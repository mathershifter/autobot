# Autobot — Console Robot Script Engine

Autobot executes YAML-defined scripts against remote consoles via pexpect. It handles login negotiation, multi-hop session management, and cleanup automatically.

## Architecture

```
YAML script → pydantic validation → Runner → Session (pexpect) → remote console
```

- **YAML script** defines environment, credentials, prompt patterns, functions, attach config, and a step-based script.
- **Pydantic models** (`Config` and subtypes) validate and parse the YAML against `schemas/autobot.2026-08.json`.
- **Runner** renders Jinja2 templates, dispatches steps, and manages block breakouts.
- **Session** wraps pexpect, handles prompt detection and credential cycling.

## YAML Script Structure

All fields validated by pydantic against `schemas/autobot.2026-08.json`.

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `autobot` | yes | Schema version in `YYYY-MM` format (e.g. `2026-08`) |
| `env` | no | String key-value defaults, overridden by OS environment variables. Supports nesting (e.g. `{{ env.OTHER_KEY }}`). Accessible as `{{ env.KEY }}` |
| `vars` | no | Arbitrary objects, accessible as `{{ vars.KEY }}` |
| `prompts` | no | Named prompt/response definitions for interactive sessions |
| `errors` | no | Regex patterns for CLI error detection (e.g. `% .*`). When defined, replaces `$?` exit code checking. |
| `fn` | no | Named functions (reusable step sequences) |
| `attach` | yes | Session spawn and lifecycle config |
| `script` | yes | Ordered list of steps to execute |

### `prompts`

Each prompt has:
- `name` — identifier
- `expect` — list of patterns to match against session output. Each entry can be a string or a list of strings (grouped alternatives).
- `return` — optional boolean; if `true`, matching this prompt means "we have a shell prompt" and the pending `cmd` is sent. Defaults to `false`. A prompt with no `send` field is also treated as a shell prompt.
- `send` — optional; responses to send when a pattern matches. Accepts three forms:
  - A flat list of strings: `["response1", "response2"]`
  - A list of lists (grouped attempts): `[["user", "pass"], ["user2", "pass2"]]`
  - A `sendEach` object for data-driven responses (see below)

#### `sendEach`

Iterates over a collection from `vars` to build responses:
```yaml
send:
  each: vars.creds
  fields: [username, password]
```

This resolves `vars.creds`, and for each item emits the named fields in order. If `fields` is omitted, each item is converted to a string directly.

### `fn`

Named functions callable from `call` steps. Each function contains a `script` array of steps:
```yaml
fn:
  is_system_running:
    script:
      - cmd: systemctl is-system-running --wait
        assert:
          - running
          - degraded
```

### `attach`

| Field | Required | Description |
|-------|----------|-------------|
| `prepare` | no | Local script to run before spawning the session (e.g. authentication, tunnel setup). Uses the shebang to determine the interpreter. Aborts if the script exits non-zero. |
| `spawn` | yes | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`) |
| `timeout` | no | Timeout for the initial spawn (duration) |
| `env` | no | Environment variables for the spawned process. Replaces the full process environment (not merged with the parent). If omitted, defaults to `TERM=dumb` and `NO_COLOR=1`. |
| `script` | no | Steps to run immediately after spawn (before main script) |
| `breakout` | no | Steps to run in `finally` after the main script (cleanup/disconnect) |

The attach lifecycle:
1. `attach.prepare` runs locally (if defined) — aborts on failure
2. `pexpect.spawn(attach.spawn)` — waits for initial output
3. `attach.script` steps execute (e.g. jump-host commands)
4. Main `script` steps execute
5. `attach.breakout.script` executes (best-effort, errors logged to stderr)
6. Session closed

## Step Types

### `cmd` — Send command(s) to the shell

Waits for a prompt, sends the command, waits for the next prompt, and checks the result.

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
```

`cmd` accepts a string or list of strings. Each line waits for a prompt before sending. A multiline string is split on newlines (blank lines are skipped).

After each command line, the step waits for a shell prompt. If top-level `errors` patterns are defined, that line's captured output is checked against them; on a match the step raises and no further lines are sent.

After the last command line, the step:
1. If `assert` is defined, checks the captured output of all lines for a matching pattern — raises if none match
2. If `assert` is not defined and no `errors` are defined, checks the return code of the last line via `echo $?` — raises on non-zero

When `assert` is defined, it replaces the return code check — the assertion pattern is the success criteria. If top-level `errors` patterns are defined, they replace the `$?` check.

#### Captured output

The captured output of a command line is the text the session prints between sending the line and the next shell prompt, with:
- The terminal echo of the sent command removed. The echo is matched ignoring whitespace and `\r`, across wrapped lines, and in readline's horizontal-scroll form (`\r<` + visible tail) used for long lines. If the echo doesn't match (e.g. echo disabled), the output is left unchanged.
- Line endings normalized to `\n`.
- The trailing partial line before the prompt match (the prompt prefix) dropped. Output that doesn't end with a newline is therefore not captured.

`errors` patterns are matched with `re.MULTILINE` against the captured output after the shell prompt returns, so they never match the echoed command, and the session is left at the prompt when the error is raised. An error that is printed without a prompt returning results in a timeout rather than an error match.

Set `ignore_error: true` to continue on failure:

```yaml
- cmd: show bogus
  ignore_error: true
```

#### Capturing output with `register`

Set `register` to store the command's captured output into `vars.<name>`, making it available to subsequent steps via Jinja2 templates as `{{ vars.<name> }}`. The stored value is the output text with leading and trailing whitespace stripped.

```yaml
- cmd: show version
  register: version_output

- cmd: "echo 'Version was: {{ vars.version_output }}'"
```

`register` works with all `cmd` forms: plain commands, command lists, multiline strings, and embedded scripts. The output is captured regardless of whether `assert`, `ignore_error`, or `errors` are in use — as long as execution continues past the step (i.e., the error is either absent or ignored).

For command lists and multiline strings, the captured output of every line is concatenated. When an error is ignored, the output captured up to and including the failing line is stored.

#### Embedded scripts

If `cmd` is a string starting with `#!`, it is treated as an embedded script. The shebang line determines the interpreter. The script is written to a temp file on the remote, made executable, executed, and cleaned up automatically.

```yaml
- cmd: |
    #!/bin/bash
    echo "hello"
    if [ -f /tmp/foo ]; then
      rm /tmp/foo
    fi
```

```yaml
- cmd: |
    #!/usr/bin/env python3
    import json
    with open("/tmp/out.json") as f:
        data = json.load(f)
    print(data["version"])
```

Jinja2 templating, `assert`, `ignore_error`, and `timeout` all work normally with embedded scripts. Embedded scripts must be a single string, not a list.

Upload mechanism:
- The rendered script (with a trailing newline preserved) is base64-encoded and sent in single-line chunks of at most 512 base64 characters, waiting for the prompt after each, to `/tmp/_autobot_<uuid>.b64`. It is then decoded to `/tmp/_autobot_<uuid>`.
- Files are created under `umask 077` and the script is made mode `700`.
- The upload is verified by byte count; a mismatch is a step failure (which `ignore_error` can swallow).
- The remote must be a POSIX shell with `base64 -d`, `tee`, and `wc`.

Cleanup:
- The script and its `.b64` staging file are always removed, even if the upload or the script fails.
- Cleanup is best-effort with a timeout of at most 10s (or the step timeout, if shorter). Its errors are logged but never replace the step's error.

### `sleep` — Pause execution

```yaml
- sleep: 60s
```

### `call` — Invoke a named function

```yaml
- call: is_system_running
```

### `block` — Named group of steps

Groups steps under a label with an optional `enter` (setup) and `breakout` (cleanup), similar to `attach`.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Label for the block |
| `prompts` | no | Prompt handlers scoped to this block. Replaces the top-level prompts for the duration of the block (restored on exit) |
| `enter` | no | Steps to run before the main script (setup) |
| `script` | no | Main steps to execute |
| `breakout` | no | Steps to run in `finally` after the main script (cleanup, best-effort) |

The block lifecycle:
1. If `prompts` is defined, swap session handlers to the block's prompts
2. `enter` steps execute (if defined)
3. `script` steps execute
4. `breakout.script` executes in `finally` (best-effort, errors logged to stderr)
5. If `prompts` was defined, restore the previous session handlers

```yaml
- block:
    name: Install SONiC
    script:
      - cmd: sonic-installer install -y image.swi
```

With enter and breakout:

```yaml
- block:
    name: Host Console
    enter:
      - cmd: consutil connect 0
    script:
      - call: is_system_running
      - cmd: show version
    breakout:
      script:
        - line: exit
```

With block-scoped prompts (e.g. a sub-console with different prompt patterns):

```yaml
- block:
    name: Host Console
    prompts:
      - name: host-cli
        expect:
          - 'admin@host:~\$'
        return: true
      - name: host-login
        expect:
          - ['login:', 'Password:']
        send:
          each: vars.creds
          fields: [username, password]
    enter:
      - cmd: consutil connect 0
    script:
      - cmd: show version
    breakout:
      script:
        - line: exit
```

### `line` — Raw send (no prompt wait)

```yaml
- line: a dut attach ldp448
```

### `return` — Send empty newline(s)

```yaml
- return: 1       # send one newline
- return: 3       # send three
```

### `control` — Send control character(s)

```yaml
- control: "]"           # Ctrl+]
- control: [a, x]        # Ctrl+A then Ctrl+X
```

## Common Step Properties

All step types except `sleep` support:

| Field | Description |
|-------|-------------|
| `after` | Expect regex — wait for this pattern before executing. On match, populates `session.before` and `session.match` |
| `when` | Jinja2 conditional — template is rendered, step is skipped if result is falsy (`""`, `"false"`, `"False"`, `"0"`, `"none"`) |
| `delay_before` | Duration to wait before the step |
| `delay_after` | Duration to wait after the step |
| `timeout` | Override default timeout for this step |

`line` and `return` steps do not support `timeout`.

These properties also apply to plugin steps. They are handled by the runner; the plugin's own model receives only its plugin-specific fields.

Order of evaluation: `after` (wait) -> `when` (decide) -> `delay_before` -> execute -> `delay_after`.

### Conditional execution with `when`

The `when` field accepts a Jinja2 template string. The rendered result is evaluated as a boolean gate: if the result is empty or one of the falsy strings (`"false"`, `"False"`, `"0"`, `"none"`), the step is skipped entirely.

The `session.before` and `session.match` context variables are populated both by `after` (explicit expect) and by `get_prompt` (on shell prompt match). Commonly used with `when`:

```yaml
- cmd: show version
  after: 'Version:'
  when: "{{ session.before | contains('SONiC') }}"
```

```yaml
- cmd: apply fix
  when: "{{ session.match | search('error|warning') }}"
```

## Duration Format

Durations accept a bare number (seconds) or a string with a unit suffix:
- `5`, `5s` — 5 seconds
- `500ms` — 500 milliseconds
- `2m` — 2 minutes
- `1h` — 1 hour

## Jinja2 Templating

All string values in `cmd`, `assert`, `attach.spawn`, `when`, and prompt `send` fields support Jinja2 templates. Available context:

| Variable | Source |
|----------|--------|
| `env` | `env` section of the YAML |
| `vars` | `vars` section of the YAML (also populated at runtime by `cmd` steps with `register`) |
| `args` | CLI `--arg KEY=VALUE` arguments |
| `session.before` | Text captured before the last `after` match (pexpect `before`), or the captured output of the last command when a shell prompt is reached (empty if it printed nothing). The `$?` check and embedded-script cleanup don't change it. |
| `session.match` | Text that matched the last `after` pattern (pexpect `after`) |

Built-in global: `range`. Use Jinja2 filters for other operations (e.g. `{{ items | length }}`).

### Custom Filters

| Filter | Usage | Description |
|--------|-------|-------------|
| `contains` | `{{ value \| contains('substring') }}` | Returns `True` if `substring` is found in `value` |
| `search` | `{{ value \| search('regex') }}` | Returns `True` if the regex pattern matches anywhere in `value` |

## Prompt Handling (`get_prompt`)

`get_prompt()` only detects and navigates to a shell prompt — it does not send commands. The caller is responsible for sending the command via `sendline()` after `get_prompt()` returns.

The prompt engine polls the session output in 5-second intervals:
1. If a prompt with no `send` (or `return: true`) matches → return (shell prompt reached)
2. If a prompt with `send` values matches → send the next response and continue waiting
3. On 5-second timeout with no match → send a single empty newline to solicit a prompt (once only, and only if no handler has been activated yet)
4. On overall timeout → raise `TimeoutError`

This handles idle consoles that need a return press to display a prompt.

## CLI

```
autobot <script.yaml> [--arg KEY=VALUE ...]
```

- `script` — path to the YAML script file
- `--arg` — pass arguments accessible as `{{ args.KEY }}`

A script that fails validation, including an empty file or a document that isn't a mapping, is reported on stderr as `Validation errors:` followed by the details, and the CLI exits with status 1.
