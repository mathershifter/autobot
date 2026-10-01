# Autobot

A console robot for automating interactive sessions over SSH, telnet, and serial consoles. Autobot handles prompt detection, credential cycling, multi-hop connections, and session cleanup.

## Install

```bash
uv tool install git+https://github.com/mathershifter/autobot.git
```

Or with pipx:

```bash
pipx install git+https://github.com/mathershifter/autobot.git
```

## Usage

```
autobot [run] <script.yaml> [-a KEY=VALUE ...]
autobot schema
```

`autobot <script.yaml>` is short for `autobot run <script.yaml>`: anything other than `run`, `schema`, `-h` or `--help` as the first argument runs a script. To run a script file named `run` or `schema`, use `autobot run schema` or `autobot ./schema`.

| Flag                              | Description                                                 |
|-----------------------------------|-------------------------------------------------------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | Pass an argument accessible as `{{ args.KEY }}` in templates. Repeat the flag for more arguments; the value is everything after the first `=`, and the last value given for a key wins |
| `-h`, `--help`                    | Show help (`autobot -h` lists the subcommands, `autobot run -h` the options) |

`autobot schema` prints the JSON schema to stdout, extended with the step types of installed plugins (see [Schema](#schema)).

A run that completes exits with status 0. If the script can't be loaded, the CLI prints one error on stderr and exits with status 1 before anything runs. That covers a missing or unreadable file, invalid YAML (reported with its line and column), a key repeated in the same mapping (YAML keys must be unique, so a second `script:` is an error, not an override), a validation failure, an `--arg` without `=`, a template error or reference cycle in the top-level `env`, a template syntax error in a top-level prompt's `send`, and a top-level `sendEach` collection that can't be resolved. A malformed command line (e.g. `-a` with no value) prints usage and exits with status 2. An error while the script runs (a failed `prepare`, a timeout, a failed step) is printed as a Python traceback on stderr, after any breakouts, and exits with status 1.

Autobot's own `>> ...` messages and errors go to stderr. The session's output is echoed to stdout, with ANSI escape sequences removed.

## Script Structure

A script is a YAML file with the following top-level fields:

```yaml
autobot: 2026-10

env:                    # string key-value defaults (overridden by OS env vars)
  IMAGE_URL: https://...

vars:                   # arbitrary data accessible as {{ vars.KEY }}
  creds:
    - username: admin
      password: secret

prompts:                # interactive prompt handlers
  - name: cli
    expect: ['\w+@\w+:.+']
    return: true

fn:                     # reusable step sequences
  check_boot:
    script:
      - cmd: systemctl is-system-running --wait
        assert: [running, degraded]

attach:                 # session spawn and lifecycle
  spawn: ssh jumphost
  script:
    - line: connect-to-device
  breakout:
    script:
      - control: "]"
      - line: q

script:                 # main steps to execute
  - cmd: show version
  - call: check_boot
```

| Field     | Required | Description                                                                                                                  |
|-----------|----------|------------------------------------------------------------------------------------------------------------------------------|
| `autobot` | yes      | Schema version: `2026-10`. Scripts written for `2026-08` need changes to their prompts, see [Migrating from 2026-08](SPEC.md#migrating-from-2026-08) |
| `env`     | no       | String key-value defaults, overridden by OS env vars (an OS value is used as written, not rendered as a template). Supports nesting in any order: `{{ env.OTHER_KEY }}`; a reference cycle (`env cycle: A -> B -> A`) is a load error. Accessible as `{{ env.KEY }}` |
| `vars`    | no       | Arbitrary objects, accessible as `{{ vars.KEY }}`                                                                            |
| `prompts` | no       | Named prompt/response definitions for interactive sessions                                                                   |
| `errors`  | no       | Regex patterns for CLI error detection (e.g. `% .*`). When defined, replaces `$?` exit code checking                         |
| `fn`      | no       | Named functions (reusable step sequences)                                                                                    |
| `attach`  | yes      | Session spawn and lifecycle config                                                                                           |
| `script`  | yes      | Ordered list of steps to execute                                                                                             |

## Attach

The `attach` block controls how autobot connects to the remote console.

| Field      | Required | Description                                                                                                                         |
|------------|----------|-------------------------------------------------------------------------------------------------------------------------------------|
| `prepare`  | no       | Local script to run before spawning (e.g. auth, tunnel setup). Uses the shebang for the interpreter. Aborts on non-zero exit        |
| `spawn`    | yes      | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`)                                                                  |
| `timeout`  | no       | Timeout for the initial spawn                                                                                                       |
| `env`      | no       | Environment variables for the spawned process. Replaces the full process env (not merged). Defaults to `TERM=dumb` and `NO_COLOR=1`; `env: {}` means an empty env. Without `PATH`, the spawn command is looked up in `/bin:/usr/bin` |
| `script`   | no       | Steps to run immediately after spawn (before main script)                                                                           |
| `breakout` | no       | Steps to run in `finally` after the main script (cleanup/disconnect)                                                                |

### Lifecycle

1. `attach.prepare` runs locally (if defined) — aborts on failure
2. `pexpect.spawn(attach.spawn)` — waits up to `attach.timeout` (default 300s) for initial output. The output is left unconsumed, so a login or shell prompt that arrives with the banner is handled by the first prompt wait.
3. `attach.script` steps execute (e.g. jump-host commands)
4. Main `script` steps execute
5. `attach.breakout.script` executes (best-effort, errors logged to stderr)
6. Session closed

The session is always closed, even if the initial spawn wait times out or the breakout fails. A breakout error never replaces an error raised by the script; the original error is what propagates.

### Example

```yaml
attach:
  prepare: |
    #!/bin/sh
    arista-ssh check-auth || arista-ssh login
  spawn: ssh jumphost
  script:
    - line: a dut attach ldp448
    - return: 1
      after: "attached to"
  timeout: 300s
  breakout:
    script:
      - line: logout
      - control: "]"
      - line: logout
```

## Prompts

Prompts define how autobot recognizes and responds to interactive patterns in the session output.

A prompt with `return: true` (or no `send` field) is a **shell prompt** — when matched, autobot knows the previous command finished and the next one can be sent. A `return: true` prompt can't have `send` (it would never be sent); the script fails validation with `return_with_send`:

```yaml
- name: cli
  expect:
    - '\w+@[\w\-\.]+:[^\$]+'
    - '(arista-)?bmc-boot=>'
  return: true
```

A prompt with `send` is an **interactive prompt**: autobot responds automatically. `expect` is a regex or a list of regexes. The regexes are alternatives, so any of them triggers the prompt. The `send` field accepts two forms.

**A string**: a simple prompt with one answer, sent on any match: whichever of its regexes matches, each time the prompt appears. It never runs out, so a question asked repeatedly is answered again. The string is a template, rendered each time it is sent. `send: ''` just presses Enter:

```yaml
- name: confirm
  expect:
    - 'continue\?'
    - 'are you sure\?'
  send: 'yes'
```

Quote the answer. Unquoted, YAML reads `yes`, `no`, `on`, `off`, `true`, `false` and numbers as booleans or numbers, and the script fails validation with `send_type`. A list is not a `send` form (`send_list`), and neither is a list inside `expect` (`grouped_expect`): a login, or any other sequence of prompts, uses `sendEach` with `fields`, with the values in `vars`.

**sendEach**: data-driven responses from `vars`, for logins and other sequences of prompts. Each `fields` entry pairs the prompt it answers (`match`: a regex, or a list of alternative regexes) with the item field it sends, so the prompt has no `expect`:

```yaml
vars:
  creds:
    - {username: admin, password: password1}
    - {username: admin, password: password2}

prompts:
  - name: login
    send:
      each: vars.creds
      fields:
        - match: ['(?:L|l)ogin:', 'Username:']
          field: username
        - match: '(?:P|p)assword:'
          field: password
```

This resolves `vars.creds`, and each item is one login attempt (credential cycling). Each entry sends its field of the current item when one of its regexes matches. When the same entry matches again (e.g. `login:` after a rejected password, or `Password:` twice), autobot moves on to the next item. A password-only login such as `ssh admin@host` works with the same prompt. If autobot must move on and no item is left, the step fails with `responses exhausted`. See [SPEC.md](SPEC.md#response-selection) for the exact rules.

Without `fields`, each item (a string, number or boolean) is sent as it is, in answer to any of the prompt's `expect` regexes:

```yaml
- name: pin
  expect: ['PIN:']
  send:
    each: vars.pins
```

`each` must be a path of keys under `vars` (e.g. `vars.creds` or `vars.site.creds`) that leads to a list. With `fields`, every item must be a mapping with each entry's field; without it, every item must be a string, number or boolean. A path or item that doesn't fit stops the script: before anything runs for the top-level prompts, or on entering the block for a block's prompts. For example: `prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'`. See [SPEC.md](SPEC.md#sendeach).

## Step Types

### `cmd` — Send command(s) to the shell

Waits for a prompt, sends the command, waits for the next prompt, and checks the result.

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
```

`cmd` accepts a string or list of strings. Each line waits for a prompt before sending. A multiline string is rendered as a template first, then split on newlines (blank lines are skipped), so a `{% for %}` loop may span lines and send one command per iteration.

After each command line, the step waits for a shell prompt and, if top-level `errors` patterns are defined, checks that line's output against them — raising (and sending no further lines) on a match.

After the last command line, the step:
1. If `assert` is defined, checks the captured output of all lines for a matching pattern — raises if none match
2. Otherwise, if no top-level `errors` are defined, checks the return code of the last line via `echo $?` — raises on non-zero

Set `ignore_error: true` to continue on failure:

```yaml
- cmd: show bogus
  ignore_error: true
```

#### Capturing output with `register`

Set `register` to store the command's captured output into `vars.<name>`, making it available to subsequent steps via Jinja2 templates as `{{ vars.<name> }}`. The stored value is stripped of leading and trailing whitespace.

```yaml
- cmd: show version
  register: version_output

- cmd: "echo 'Version was: {{ vars.version_output }}'"
```

`register` works with all `cmd` forms: plain commands, command lists, multiline strings, and embedded scripts. For lists and multiline strings, the output of every line is concatenated. Output is captured as long as execution continues past the step (i.e., no unignored error); when an error is ignored, `register` stores the output captured up to the failure.

#### What counts as output

Captured output (used by `register`, `assert`, `errors`, and `session.before`) is what the command printed:

- The terminal echo of the sent command is removed. If the echo doesn't match the sent line (e.g. echo disabled with `stty -echo`), the output is left as is.
- Line breaks are preserved as `\n`.
- ANSI escape sequences (colors, cursor movement) are removed, so `assert`, `errors` and `register` see plain text. `after` and prompt `expect` regexes, on the other hand, match the raw output, escape sequences included; see [ANSI escape sequences](SPEC.md#ansi-escape-sequences).
- The prompt line is excluded. Because of that, any text printed without a trailing newline (it shares a line with the next prompt) is not captured — e.g. `printf 'x\ny'` captures `x`.

**Important:** `cmd` blocks until a prompt appears after the command. For commands that won't return a prompt (e.g. `reboot`, `exit`), use `line` instead.

#### Multi-line commands

As a list (each entry sent separately):

```yaml
- cmd:
    - configure terminal
    - interface Ethernet1
    - shutdown
```

Or as a multiline string (split on newlines automatically):

```yaml
- cmd: |
    configure terminal
    interface Ethernet1
    shutdown
```

#### Embedded scripts

If `cmd` is a string starting with `#!`, it is treated as an embedded script. The shebang determines the interpreter. The script is written to a temp file on the remote, made executable, executed, and cleaned up automatically.

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
    console.print(data["version"])
```

Jinja2 templating, `assert`, `ignore_error`, and `timeout` all work normally with embedded scripts. Embedded scripts must be a single string, not a list.

The script is uploaded base64-encoded, in lines of at most 512 base64 characters, to `/tmp/_autobot_<random>` (mode `700`, created under `umask 077`). The remote shell must be POSIX-compatible and provide `base64 -d`, `tee`, and `wc`. The upload is checked by comparing the decoded byte count to the script's length. The temp file is always removed, even if the upload or the script fails. Removal is best-effort: it waits at most 10 seconds (or the step timeout, if that's shorter) for the prompt, and a failed cleanup is logged without replacing the step's own error.

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

| Field      | Required | Description                                                            |
|------------|----------|------------------------------------------------------------------------|
| `name`     | yes      | Label for the block                                                    |
| `prompts`  | no       | Prompt handlers scoped to this block. Replaces top-level prompts for the duration of the block (restored on exit) |
| `enter`    | no       | Steps to run before the main script (setup)                            |
| `script`   | no       | Main steps to execute                                                  |
| `breakout` | no       | Steps to run in `finally` after the main script (cleanup, best-effort) |

The block lifecycle:
1. If `prompts` is defined, swap session handlers to the block's prompts
2. `enter` steps execute (if defined)
3. `script` steps execute
4. `breakout.script` executes in `finally` (best-effort, errors logged to stderr)
5. If `prompts` was defined, restore the previous session handlers

Steps 4 and 5 run even if `enter` fails, and step 5 runs even if the breakout fails. A breakout error never replaces an error raised by `enter` or `script`.

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

### `line` — Raw send (no prompt wait)

Sends text without waiting for a prompt before or after. Use for commands that won't produce a standard prompt response (e.g. `reboot`, interactive sub-sessions).

```yaml
- line: sudo reboot now
  delay_after: 10s
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

All step types except `sleep` support these optional fields:

| Field          | Description                                                    |
|----------------|----------------------------------------------------------------|
| `after`        | Regex pattern — wait for this to appear in output before executing. On match, populates `session.before` and `session.match` |
| `when`         | Jinja2 conditional — step is skipped if the rendered result, stripped and lowercased, is `""`, `false`, `0` or `none` (so `False`, `None` and `" FALSE "` also skip) |
| `delay_before` | Duration to wait before the step                               |
| `delay_after`  | Duration to wait after the step                                |
| `timeout`      | Bounds each wait of this step (default 300s), not the step as a whole |

`line` and `return` steps do not support `timeout`.

For `cmd`, `timeout` applies separately to each wait: `after`, each prompt wait, the `$?` check and the embedded-script upload. For `call`, `block` and `control` it bounds only the `after` wait: the steps inside a function or block keep their own `timeout` (default 300s) and don't inherit it.

To leave an optional field at its default, omit the key. An empty value such as `after:` or `timeout: ~` is `null`, which is a validation error for every optional field.

## Duration Format

Durations accept a bare number (seconds) or a string with a unit suffix:

```
5       # 5 seconds (bare number)
500ms   # milliseconds
30s     # seconds
2m      # minutes
1h      # hours
```

Durations can't be negative, and `true`/`false` and `null` (e.g. a bare `sleep:`) aren't durations.

## Templating

These values are [Jinja2](https://jinja.palletsprojects.com/) templates: `cmd` (including embedded scripts), `assert`, `line`, `after`, `when`, prompt `send` strings (rendered each time one is sent, so they can use `vars` registered by earlier steps; not the values `sendEach` reads from `vars`), `attach.spawn`, `attach.prepare`, and `env` values. Other values, such as `expect`, `errors` and `attach.env`, are used as written.

```yaml
env:
  BASE_URL: https://artifacts.example.com
  IMAGE: '{{ env.BASE_URL }}/sonic-broadcom.swi'

script:
  - cmd: wget -P /tmp {{ env.IMAGE }}
  - cmd: echo {{ args.message }}
```

Since these values are templates, `{{`, `{%` and `{#` always start Jinja2 syntax, even in shell code: bash's `${#arr[@]}` fails to render because `{#` opens a Jinja2 comment. Wrap such text in `{% raw %}...{% endraw %}` (or write `{{ '{#' }}`):

```yaml
- cmd: |
    #!/bin/bash
    arr=(a b c)
    {% raw %}echo "${#arr[@]}"{% endraw %}
```

Available context:

| Variable         | Source                                  |
|------------------|-----------------------------------------|
| `env.*`          | `env` section (merged with OS env vars) |
| `vars.*`         | `vars` section (also populated at runtime by `cmd` steps with `register`) |
| `args.*`         | CLI `--arg` flags                       |
| `session.before` | Text before the last `after` match, or the captured output of the last command (see [What counts as output](#what-counts-as-output)) |
| `session.match`  | Text that matched the last `after` pattern or shell prompt |

## Error Handling

By default, `cmd` steps check the return code via `echo $?` and raise on non-zero. You can change this behavior in two ways:

**Per-step:** Set `ignore_error: true` to log and continue:

```yaml
- cmd: show bogus
  ignore_error: true
```

`ignore_error` covers command failures only: a non-zero exit code, a failed `assert`, an `errors` match, and an embedded-script upload mismatch. Timeouts, a closed connection, template errors and prompt-response failures (`responses exhausted`) always abort the script.

**Global error patterns:** Define top-level `errors` to detect errors by output pattern instead of exit code. This is useful for CLIs that don't use standard exit codes (e.g. Arista EOS):

```yaml
errors:
  - '% .*'

script:
  - cmd: show bogus    # raises because output matches '% .*'
```

Patterns are matched per line (`re.MULTILINE`, so `.` doesn't cross line breaks) against the captured output once the prompt returns. The echoed command itself is never matched, so a comment like `! note` in a command doesn't trigger `'! .*'`.

## Functions

Define reusable step sequences in `fn` and invoke them with `call`:

```yaml
fn:
  is_system_running:
    script:
      - cmd: systemctl is-system-running --wait
        assert:
          - running
          - degraded

  reboot:
    script:
      - line: sudo reboot now
        delay_after: 10s

script:
  - call: is_system_running
  - cmd: sonic-installer install -y image.swi
  - call: reboot
  - call: is_system_running
```

Every `call` target must be defined in `fn`. This is checked when the script is loaded, like a misspelled field in a plugin step, so a typo is reported as a validation error before `prepare` runs or anything connects.

## Schema

The full JSON Schema is in [`schemas/autobot.2026-10.json`](schemas/autobot.2026-10.json). `autobot schema` prints it, with a definition added for each installed plugin step. It reads the file from the source tree when it runs from a checkout; an installed copy downloads it from the `main` branch on GitHub. It is normative: autobot accepts the scripts the schema accepts. The exception is step keys: the static schema accepts any unknown step key as a possible plugin step, while autobot rejects a key that no installed plugin provides.

For the detailed specification, see [`SPEC.md`](SPEC.md).
