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
autobot <script.yaml> [--arg KEY=VALUE ...]
```

| Flag             | Description                                                 |
|------------------|-------------------------------------------------------------|
| `--arg KEY=VALUE` | Pass arguments accessible as `{{ args.KEY }}` in templates |

## Script Structure

A script is a YAML file with the following top-level fields:

```yaml
autobot: 2026-08

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
| `autobot` | yes      | Semver string (e.g. `20206-08`)                                                                                                 |
| `env`     | no       | String key-value defaults, overridden by OS env vars. Supports nesting: `{{ env.OTHER_KEY }}`. Accessible as `{{ env.KEY }}` |
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
| `env`      | no       | Environment variables for the spawned process. Replaces the full process env (not merged). Defaults to `TERM=dumb` and `NO_COLOR=1` |
| `script`   | no       | Steps to run immediately after spawn (before main script)                                                                           |
| `breakout` | no       | Steps to run in `finally` after the main script (cleanup/disconnect)                                                                |

### Lifecycle

1. `attach.prepare` runs locally (if defined) — aborts on failure
2. `pexpect.spawn(attach.spawn)` — waits for initial output
3. `attach.script` steps execute (e.g. jump-host commands)
4. Main `script` steps execute
5. `attach.breakout.script` executes (best-effort, errors logged to stderr)
6. Session closed

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
      when: "attached to"
  timeout: 300s
  breakout:
    script:
      - line: logout
      - control: "]"
      - line: logout
```

## Prompts

Prompts define how autobot recognizes and responds to interactive patterns in the session output.

A prompt with `return: true` (or no `send` field) is a **shell prompt** — when matched, autobot knows the previous command finished and the next one can be sent:

```yaml
- name: cli
  expect:
    - '\w+@[\w\-\.]+:[^\$]+'
    - '(arista-)?bmc-boot=>'
  return: true
```

A prompt with `send` is an **interactive prompt** — autobot responds automatically:

```yaml
- name: login
  expect:
    - ['(?:L|l)ogin:', '(?:P|p)assword:']
  send:
    each: vars.creds
    fields: [username, password]
```

The `expect` field is a list of patterns. Each entry can be a string or a list of strings (grouped alternatives). Grouped entries are matched together — when the first pattern in a group matches, autobot sends the first response; when the second matches, it sends the second, and so on.

### Send forms

The `send` field accepts three forms:

**Flat list** — responses sent in order as patterns match:

```yaml
send: ["admin", "password"]
```

**List of lists** — grouped attempts (credential cycling):

```yaml
send:
  - ["admin", "password1"]
  - ["admin", "password2"]
```

**sendEach** — data-driven responses from `vars`:

```yaml
send:
  each: vars.creds
  fields: [username, password]
```

This resolves `vars.creds`, and for each item emits the named fields in order.

## Step Types

### `cmd` — Send command(s) to the shell

Waits for a prompt, sends the command, waits for the next prompt, and checks the result.

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
```

`cmd` accepts a string or list of strings. Each line waits for a prompt before sending. A multiline string is split on newlines (blank lines are skipped).

After the last command line, the step:
1. Waits for a shell prompt (confirming the command finished)
2. Checks the return code via `echo $?` — raises on non-zero
3. If top-level `errors` patterns are defined, checks command output against those patterns instead of `$?`

Set `ignore_error: true` to continue on failure:

```yaml
- cmd: show bogus
  ignore_error: true
```

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
    print(data["version"])
```

Jinja2 templating, `assert`, `ignore_error`, and `timeout` all work normally with embedded scripts. Embedded scripts must be a single string, not a list.

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
| `enter`    | no       | Steps to run before the main script (setup)                            |
| `script`   | no       | Main steps to execute                                                  |
| `breakout` | no       | Steps to run in `finally` after the main script (cleanup, best-effort) |

The block lifecycle:
1. `enter` steps execute (if defined)
2. `script` steps execute
3. `breakout.script` executes in `finally` (best-effort, errors logged to stderr)

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
| `when`         | Regex pattern — wait for this to appear in output before executing |
| `delay_before` | Duration to wait before the step                               |
| `delay_after`  | Duration to wait after the step                                |
| `timeout`      | Override default timeout for this step                         |

`line` and `return` steps do not support `timeout`.

## Duration Format

Durations accept a bare number (seconds) or a string with a unit suffix:

```
5       # 5 seconds (bare number)
500ms   # milliseconds
30s     # seconds
2m      # minutes
1h      # hours
```

## Templating

All string values support [Jinja2](https://jinja.palletsprojects.com/) templates:

```yaml
env:
  BASE_URL: https://artifacts.example.com
  IMAGE: '{{ env.BASE_URL }}/sonic-broadcom.swi'

script:
  - cmd: wget -P /tmp {{ env.IMAGE }}
  - cmd: echo {{ args.message }}
```

Available context:

| Variable | Source                                  |
|----------|-----------------------------------------|
| `env.*`  | `env` section (merged with OS env vars) |
| `vars.*` | `vars` section                          |
| `args.*` | CLI `--arg` flags                       |

## Error Handling

By default, `cmd` steps check the return code via `echo $?` and raise on non-zero. You can change this behavior in two ways:

**Per-step:** Set `ignore_error: true` to log and continue:

```yaml
- cmd: show bogus
  ignore_error: true
```

**Global error patterns:** Define top-level `errors` to detect errors by output pattern instead of exit code. This is useful for CLIs that don't use standard exit codes (e.g. Arista EOS):

```yaml
errors:
  - '% .*'

script:
  - cmd: show bogus    # raises because output matches '% .*'
```

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

## Schema

The full JSON Schema is in [`autobot.schema.json`](autobot.schema.json).

For the detailed specification, see [`SPEC.md`](SPEC.md).
