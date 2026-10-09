# Autobot

Autobot runs a YAML script against an interactive console: SSH, telnet, or a serial console behind a console server. It waits for prompts, answers logins, sends commands, checks their results, and logs out when the run ends.

This README is the introduction and quick reference. [SPEC.md](SPEC.md) has the exact rules.

## Install

```bash
uv tool install git+https://github.com/mathershifter/autobot.git
```

Or `pipx install git+https://github.com/mathershifter/autobot.git`. Autobot needs Python 3.12 or later.

## Quick start

Save this as `hello.autobot.yaml`:

```yaml
autobot: 2026-10

vars:
  creds:
    - {username: admin, password: secret}

prompts:
  - name: cli                 # a shell prompt: the device is ready for a command
    expect: '^\w+@[\w.-]+:[^\r\n]*[$#] ?$'
    return: true
  - name: login               # answered whenever it appears
    send:
      each: vars.creds
      fields:
        - match: '[Ll]ogin: ?$'
          field: username
        - match: '[Pp]assword: ?$'
          field: password

attach:
  spawn: ssh admin@{{ args.host }}
  breakout:
    - line: exit              # runs when the script ends, also after a failure

script:
  - cmd: show version
    assert: 'SONiC Software Version'
```

Run it:

```
$ autobot hello.autobot.yaml -a host=sonic
>> attach: ssh admin@sonic
admin@sonic's password: 
>> prompt answered: login
admin@sonic:~$ 
>> cmd: show version
show version
SONiC Software Version: SONiC.4.2.0
admin@sonic:~$ 
>> breakout: detaching
>> line sent
>> run completed
```

Lines that start with `>>` are Autobot's own. The rest is the device's output. Complete scripts are in [`examples/`](examples/).

## Command line

```
autobot [run] <script.yaml> [-a KEY=VALUE ...] [--traceback]
autobot validate <script.yaml> [<script.yaml> ...] [-q] [--traceback]
autobot schema [--traceback]
```

| Command | What it does |
|---------|--------------|
| `autobot <script>` | Runs the script. Short for `autobot run <script>` |
| `autobot validate <script>...` | Checks each script without running it |
| `autobot schema` | Prints the JSON schema, with the step types of installed plugins |

| Flag | Description |
|------|-------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | Sets `{{ args.KEY }}` for templates. Repeat the flag for more arguments. `run` only |
| `-q`, `--quiet` | Prints nothing for a valid script. `validate` only |
| `--traceback` | Also prints the Python traceback of an error that is reported without one |
| `-h`, `--help` | Shows help |

### Checking a script

`autobot validate` reads and validates each script as `run` does, and stops there. It renders no template and spawns nothing, so it fits an editor hook or a CI job.

```
$ autobot validate upgrade.autobot.yaml login.autobot.yaml
upgrade.autobot.yaml: valid
Validation errors:
  script.0.cmd.timout: Extra inputs are not permitted [extra_forbidden]
login.autobot.yaml: invalid
```

The exit status is 0 when every script is valid and 1 when any isn't. Two things to know:

- A valid script can still fail when it runs. Templates are rendered only by `run`, and the device has its own say.
- Installed plugins are loaded, so their import code runs. Validate with plugins you trust.

See [SPEC.md](SPEC.md#checking-scripts-with-validate).

### Output

The session's output goes to stdout, with ANSI escape sequences removed. Autobot's `>>` messages and error reports go to stderr. So `autobot script.yaml > device.log` keeps the device's transcript, and `2> run.log` keeps what Autobot did.

Autobot never prints what a `line` step or a prompt answer sent, since it may be a password. It prints `>> line sent` and `>> prompt answered: <name>`.

On a terminal the messages are colored. Set `NO_COLOR` to turn that off, or `FORCE_COLOR` to keep it in a pipe. The full list of messages is in [SPEC.md](SPEC.md#output).

### Errors and exit status

A script that can't be loaded (an unreadable file, invalid YAML, validation errors, a plugin that fails to load) is reported on stderr, and nothing runs. An error during the run is reported after the breakouts have run and the session is closed:

```
Run failed in upgrade.autobot.yaml: timed out after 30.0s waiting for a shell prompt ('sonic')
  at script.3.block.script.1 (cmd: sonic-installer install -y image.swi)
```

The `at` line is the step's path in the script: here, the second step of the block that is the fourth step of `script`.

| Status | Meaning |
|--------|---------|
| 0 | The run completed; `validate` found every script valid |
| 1 | The script couldn't be loaded, and nothing ran; `validate` found a script invalid |
| 2 | Malformed command line |
| 3 | The run failed, or its output could not be written |
| 4 | The script completed, but a breakout didn't finish: the session may still be logged in |
| 70 | Unexpected error: a bug in Autobot or in a plugin |
| 130 | Interrupted with Ctrl-C, after the breakouts ran |
| 143, 129 | Ended by `SIGTERM` or `SIGHUP`, without the breakouts |

To stop a run and have it log out, use Ctrl-C or `kill -INT <pid>`. Three endings run no breakout, so the device may be left logged in: `SIGTERM` (a plain `kill` or `timeout`), `SIGHUP` (a closed terminal), and output that can't be written (`autobot script.yaml | head -1`). Run a long job in `tmux` or under `nohup`, and send the output to a file or to `tee`.

See [Errors while the script runs](SPEC.md#errors-while-the-script-runs) and [A run that ends without its breakouts](SPEC.md#a-run-that-ends-without-its-breakouts).

## Script structure

| Field     | Required | Description |
|-----------|----------|-------------|
| `autobot` | yes      | Schema version: `2026-10` |
| `env`     | no       | Defaults for environment variables, read as `{{ env.KEY }}`. A variable that the environment sets keeps its value |
| `vars`    | no       | Any data, read as `{{ vars.KEY }}` |
| `prompts` | no       | Prompts to recognize and answer. See [Prompts](#prompts) |
| `errors`  | no       | Regex patterns for CLI error detection (e.g. `% .*`). When defined, replaces `$?` exit code checking |
| `fn`      | no       | Named functions, run with `call`. See [Functions](#functions) |
| `attach`  | yes      | How to connect and disconnect. See [Attach](#attach) |
| `script`  | yes      | The steps to run. See [Steps](#steps) |

A key that is repeated in a mapping is an error, not an override.

## Attach

| Field      | Required | Description |
|------------|----------|-------------|
| `prepare`  | no       | Local script to run before the spawn, e.g. to authenticate. A non-zero exit aborts the run |
| `spawn`    | yes      | The command to spawn, e.g. `ssh host` or `telnet host port` |
| `timeout`  | no       | How long to wait for the first output of the spawned command (default 300s) |
| `script`   | no       | Steps to run right after the spawn, before the main `script` |
| `breakout` | no       | Steps to run after the main `script`, to clean up and disconnect |

A run goes in this order:

1. `prepare` runs locally.
2. `spawn` starts, and Autobot waits for its first output.
3. `attach.script` runs.
4. The main `script` runs.
5. `attach.breakout` runs, also when step 3 or 4 failed.
6. The session is closed.

If the spawn fails (step 2), no step runs after it, the breakout included. A breakout error never replaces the script's error. A breakout that fails after a script that completed fails the run with exit status 4.

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
  breakout:
    - control: c
      delay_before: 2s
    - line: logout
    - control: "]"
      after: '[Ll]ogin: ?$'
      timeout: 30s
    - line: logout
```

### Logging out

Over plain `ssh`, closing the connection ends the login. Behind a console server it doesn't: the console stays logged in for whoever attaches next. Autobot never sends a logout of its own, so the breakout must do it. The breakout above does it in three parts:

1. `control: c` clears the line, in case the script failed in the middle of a command. The `delay_before` lets the device read what was sent before, because Ctrl-C discards unread input.
2. `line: logout` logs out. Use `line`, not `cmd`: a `cmd` would wait for a shell prompt, and the login prompt would be answered with the credentials again.
3. The next step waits for the login prompt with `after`. End the pattern with `$`, so that `Last login: ...` in a banner doesn't match, and give the wait a `timeout`.

If the login prompt doesn't come, the breakout fails and the run reports `Session may be left logged in`. A run that answered a `sendEach` prompt and finished no breakout after it logs a `>> no logout` warning. See [SPEC.md](SPEC.md#logging-out).

### `prepare` as an rc script

A `prepare` script that a shell sources works like an rc file. The variables it exports are set for the rest of the run: templates read them through `env`, and the spawned command inherits them.

```yaml
attach:
  prepare: |
    . ~/.config/lab/credentials.sh
    export JUMP_HOST=$(lab-inventory jump-host)
  spawn: ssh -J {{ env.JUMP_HOST }} admin@{{ args.host }}
```

- A script with no shebang is sourced by `/bin/sh`. One whose shebang names `sh`, `bash`, `dash`, `ksh` or `zsh`, with no argument or with `set` options such as `-eu`, is sourced by that shell. Any other script runs as a program and sets nothing.
- The spawned command gets Autobot's own environment, then `TERM=dumb` and `NO_COLOR=1`, then what `prepare` changed. The defaults of the `env` section are for templates only and are not passed on.
- The rendered `spawn` line is printed (`>> attach: ...`). Leave a secret in the environment for the command to inherit; don't put it in the line with a template.

See [SPEC.md](SPEC.md#prepare-as-an-rc-script) and [The environment of the spawned process](SPEC.md#the-environment-of-the-spawned-process). The spawned command runs on a terminal of 500 rows by 80 columns; see [The window of the spawned process](SPEC.md#the-window-of-the-spawned-process).

## Prompts

A prompt is a name and one or more regexes that Autobot looks for in the session's output. There are two kinds.

**A shell prompt** has `return: true` (or no `send`). When it matches, the previous command is done and the next one can be sent.

```yaml
prompts:
  - name: cli
    expect:
      - '^\w+@[\w.-]+:[^\r\n]*[$#] ?$'
      - '^(arista-)?bmc-boot=> ?$'
    return: true
```

Start the regex with `^` and end it at the prompt character. An unanchored regex also matches command output that only looks like a prompt, and a regex that stops short leaves the rest of the prompt in the next command's output. See [Prompt Handling](SPEC.md#prompt-handling-get_prompt).

**An interactive prompt** has `send`, and Autobot answers it whenever it appears. A string is one fixed answer:

```yaml
  - name: confirm
    expect:
      - 'continue\?'
      - 'are you sure\?'
    send: 'yes'
```

Quote the answer. Unquoted, YAML reads `yes`, `no`, `on`, `off`, `true`, `false` and numbers as booleans or numbers, and the script fails validation. `send: ''` just presses Enter.

**`sendEach`** takes its answers from a list in `vars`. Use it for logins. Each `fields` entry pairs a prompt regex (`match`) with the item field to send, so the prompt has no `expect`:

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
        - match: ['[Ll]ogin: ?$', 'Username: ?$']
          field: username
        - match: '[Pp]assword: ?$'
          field: password
```

Each item is one login attempt. When the same entry matches again, for example `Password:` after a rejected password, Autobot moves on to the next item. When no item is left, the step fails with `responses exhausted`. End a login regex with `$`: without it, the `Last login: ...` line is answered as a second login prompt.

Without `fields`, each item is a string or number, sent in answer to the prompt's `expect`. See [SPEC.md](SPEC.md#sendeach) for the exact rules.

## Steps

| Step | What it does |
|------|--------------|
| [`cmd`](#cmd) | Waits for a shell prompt, sends a command, waits for the next prompt, and checks the result |
| [`line`](#line-return-and-control) | Sends text without waiting for a prompt |
| [`return`](#line-return-and-control) | Sends empty newlines |
| [`control`](#line-return-and-control) | Sends control characters |
| `sleep` | Pauses: `- sleep: 60s` |
| `call` | Runs a function: `- call: check_boot`. See [Functions](#functions) |
| [`block`](#block) | A named group of steps with its own setup, cleanup and prompts |

### `cmd`

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  register: version
  timeout: 30s
```

| Field | Description |
|-------|-------------|
| `cmd` | A command, a list of commands, or a multiline string with one command per line. Each line is sent at its own prompt |
| `assert` | A regex or list of regexes. The step fails if none matches the output |
| `ignore_error` | `true` logs a command failure and goes on |
| `register` | Stores the output, stripped of surrounding whitespace, as `{{ vars.<name> }}` |

After the last line, the step checks the result. With `assert`, the output must match. Otherwise the exit code is read with `echo $?` and must be 0, unless top-level `errors` patterns are defined (see [Error handling](#error-handling)).

The output is what the command printed: the echo of the command, the prompt and ANSI escape sequences are removed. Text printed without a final newline shares a line with the prompt and is not captured. See [Captured output](SPEC.md#captured-output).

A `cmd` blocks until a shell prompt comes back. For a command that doesn't return one (`reboot`, `exit`, connecting to an idle console), use `line`.

Several commands, as a list or as a multiline string:

```yaml
- cmd:
    - configure terminal
    - interface Ethernet1
    - shutdown

- cmd: |
    configure terminal
    interface Ethernet1
    shutdown
```

**Embedded scripts.** A `cmd` string that starts with `#!` is uploaded to a temp file on the remote, run with the interpreter of its shebang, and removed afterwards. The remote shell must be POSIX-compatible and have `base64 -d`, `tee` and `wc`.

```yaml
- cmd: |
    #!/usr/bin/env python3
    import json
    with open("/tmp/out.json") as f:
        print(json.load(f)["version"])
```

See [Embedded scripts](SPEC.md#embedded-scripts), and [The length of a sent line](SPEC.md#the-length-of-a-sent-line) for very long commands.

### `line`, `return` and `control`

These send at once, without waiting for a prompt before or after.

```yaml
- line: sudo reboot now     # text and a newline
  delay_after: 10s
- return: 3                 # three empty newlines
- control: "]"              # Ctrl-]
- control: [a, x]           # Ctrl-A, then Ctrl-X
```

A `control` value is one letter or one of ``@ ` [ { \ | ] } ^ ~ _ ?``. Ctrl-C, Ctrl-\\ and Ctrl-Z make the far side discard input it has not read yet, so give such a step an `after` or a `delay_before` when it follows a `line`. See [SPEC.md](SPEC.md#control--send-control-characters).

### `block`

A block groups steps under a name. Its `breakout` runs after its `script`, also when `enter` or `script` failed.

| Field      | Required | Description |
|------------|----------|-------------|
| `name`     | yes      | Label for the block |
| `prompts`  | no       | Prompts that replace the top-level ones until the block ends |
| `enter`    | no       | Steps to run first (setup) |
| `script`   | no       | The main steps |
| `breakout` | no       | Steps to run last (cleanup) |

```yaml
- block:
    name: Host Console
    enter:
      - line: consutil connect 0
    script:
      - call: is_system_running
      - cmd: show version
    breakout:
      - line: exit
```

Enter a sub-console with `line`, not `cmd`: a `cmd` would wait for a prompt that an idle console shows only after Return is pressed. The script goes on after a block whose breakout failed, but the run then does not end with status 0. See [SPEC.md](SPEC.md#block--named-group-of-steps).

## Common step properties

Every step type except `sleep` takes these optional fields:

| Field          | Description |
|----------------|-------------|
| `after`        | Regex to wait for in the output before the step runs. Sets `session.before` and `session.match` |
| `when`         | Template. The step is skipped if it renders to an empty string, `false`, `0` or `none` (in any letter case) |
| `delay_before` | Duration to wait before the step |
| `delay_after`  | Duration to wait after the step |
| `timeout`      | Limit for each wait and each send of the step (default 300s), not for the step as a whole. Not on `line` and `return` |

They apply in this order: `after`, `when`, `delay_before`, the step, `delay_after`.

On a `call` or `block`, `timeout` bounds only the `after` wait; the steps inside keep their own. To leave a field at its default, omit the key: an empty value such as `after:` is a validation error. See [SPEC.md](SPEC.md#common-step-properties).

## Durations

A duration is a number of seconds (`5`, `0.5`) or a number with a unit: `500ms`, `30s`, `2m`, `1h`.

## Templating

These values are [Jinja2](https://jinja.palletsprojects.com/) templates: `cmd`, `assert`, `line`, `after`, `when`, a prompt's `send` string, `attach.spawn`, `attach.prepare` and `env` values. Other values, such as `expect` and `errors`, are used as written.

```yaml
env:
  BASE_URL: https://artifacts.example.com
  IMAGE: '{{ env.BASE_URL }}/sonic-broadcom.swi'

script:
  - cmd: wget -P /tmp {{ env.IMAGE }}
  - cmd: show version
    register: version
  - cmd: sonic-installer install -y /tmp/sonic-broadcom.swi
    when: "{{ vars.version | contains('SONiC.4.1') }}"
```

| Variable         | Value |
|------------------|-------|
| `env.*`          | The whole environment, as `attach.prepare` changed it, plus the `env` section's defaults for variables that aren't set |
| `vars.*`         | The `vars` section, plus what `register` stored |
| `args.*`         | The `--arg` values |
| `session.before` | The text before the last `after` match, or the output of the last command |
| `session.match`  | The text that matched the last `after` pattern or shell prompt |

Two filters are added to Jinja2's own: `contains('text')` and `search('regex')`.

Things to watch for:

- A variable that isn't set is a template error. `{{ env.X | default('y') }}` gives a fallback.
- `{{`, `{%` and `{#` always start Jinja2 syntax, even in shell code. Bash's `${#arr[@]}` fails to render; wrap it in `{% raw %}...{% endraw %}`.
- An expression that gives a boolean can't be sent or stored as text. Use it in `when` or `{% if %}`, or say which text you mean: `{{ 'on' if vars.debug else 'off' }}`.

See [SPEC.md](SPEC.md#jinja2-templating).

## Error handling

By default, a `cmd` step reads the exit code with `echo $?` and fails on non-zero.

**Per step:** `ignore_error: true` logs the failure and goes on.

```yaml
- cmd: show bogus
  ignore_error: true
```

It covers command failures only: a non-zero exit code, a failed `assert`, an `errors` match and an embedded-script upload mismatch. A timeout, a closed connection or a template error always ends the script.

**Global error patterns:** top-level `errors` detects errors by output pattern instead of exit code, for CLIs that have no exit codes (e.g. Arista EOS):

```yaml
errors:
  - '% .*'

script:
  - cmd: show bogus    # fails: the output matches '% .*'
```

The patterns are matched against each command's output, never against the echoed command.

## Functions

Define a sequence of steps once in `fn` and run it with `call`. A `call` to a function that isn't defined is a validation error.

```yaml
fn:
  is_system_running:
    script:
      - cmd: systemctl is-system-running --wait
        assert: [running, degraded]

script:
  - call: is_system_running
  - cmd: sonic-installer install -y image.swi
  - line: sudo reboot now
    delay_after: 10s
  - call: is_system_running
```

## Plugins

A plugin adds a step type. It is an executor class registered in the `autobot.steps` entry-point group, with:

- `key`: the step's YAML key,
- `model`: a pydantic model of the step's own fields,
- `execute(step, ctx, timeout)`: what the step does.

`ctx` gives the plugin the session (`ctx.session`), the config (`ctx.config`), `ctx.render(template)`, `ctx.run_steps(steps)` and, for a cleanup of its own, `ctx.run_breakout(steps, what)`.

- `ctx.session.sendline(text, timeout=timeout)` sends a command; follow it with `ctx.session.get_prompt(...)`. With `solicit=True` it is a raw send, like a `line` step.
- `ctx.session.sendcontrol(char, timeout=timeout)` sends a control character.
- Raise `autobot.types.RunError` for what the device did, or `autobot.types.ScriptError` for a bad value in the script. Most other exceptions from the plugin's own code are reported as a bug in the plugin.

A plugin's key can't be a built-in step key or a common step property name. See [SPEC.md](SPEC.md#common-step-properties).

## Schema and reference

The JSON Schema is in [`schemas/autobot.2026-10.json`](schemas/autobot.2026-10.json). `autobot schema` prints it with a definition for each installed plugin step, and needs no network.

For everything else, see [`SPEC.md`](SPEC.md).
