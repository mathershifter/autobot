# Autobot

Autobot runs a YAML script against an interactive console: SSH, telnet, or a serial console behind a console server. It waits for prompts, answers logins, sends commands, checks their results, and runs the cleanup steps you give it (the breakout, such as a logout) when the run ends.

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

env:
  SWITCH:                     # required

vars:
  creds:
    - {username: admin, password: secret}

prompts:
  - name: cli                 # a shell prompt: the device is ready for a command
    expect: '^\w+@[\w.-]+:[^\r\n]*[$#] ?$'
    return: true
  - name: login               # answered when it appears while a cmd waits for a shell prompt
    send:
      each: vars.creds
      fields:
        - match: '[Ll]ogin: ?$'
          field: username
        - match: '[Pp]assword: ?$'
          field: password

attach:
  spawn: ssh admin@{{ env.SWITCH }}
  breakout:
    - line: exit              # runs when the script ends, also after a failure

script:
  - cmd: show version
    assert: 'SONiC Software Version'
```

Run it:

```
$ autobot hello.autobot.yaml -e SWITCH=sonic
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

Lines that start with `>>` are Autobot's own. The rest is the device's output. The script assumes that `ssh` already knows the host's key: nothing in it answers ssh's `yes/no` question, and the run would time out there. Complete scripts are in [`examples/`](examples/).

## Command line

```
autobot [run] <script.yaml> [-e KEY=VALUE ...] [--traceback]
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
| `-e KEY=VALUE`, `--env KEY=VALUE` | Sets a variable of the script's `env` section. Repeatable. `run` only |
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

Autobot's messages never show what a `line` step or a prompt answer sent, since it may be a password: they say `>> line sent` and `>> prompt answered: <name>`. The text shows only as far as the device echoes it. A `cmd` line is printed as rendered (`>> cmd: ...`), so a secret that a template puts into a `cmd` shows.

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

To stop a run and have it log out, use Ctrl-C or `kill -INT <pid>`. Three endings run no breakout: `SIGTERM` (a plain `kill` or `timeout`), `SIGHUP` (a closed terminal), and output that can't be written (`autobot script.yaml | head -1`). After them the device may be left logged in, with a command still running or an embedded script's temp file left on it. Run a long job in `tmux` or under `nohup`, and send the output to a file. Ctrl-C on a pipeline into `tee` ends `tee` too, and the run then ends at the breakout's first output, without the rest of the breakout; pipe into `tee -i`, or stop such a run with `kill -INT <pid>` to the `autobot` process alone.

See [Errors while the script runs](SPEC.md#errors-while-the-script-runs) and [A run that ends without its breakouts](SPEC.md#a-run-that-ends-without-its-breakouts).

## Script structure

| Field     | Required | Description |
|-----------|----------|-------------|
| `autobot` | yes      | Schema version: `2026-10` |
| `prepare` | no       | A local shell script that runs first and sets `env` variables. See [Prepare](#prepare) |
| `env`     | no       | The script's variables and their defaults, under non-empty names, read as `{{ env.KEY }}`. See [Templating](#templating) |
| `vars`    | no       | Any data under non-empty keys, read as `{{ vars.KEY }}`. Used as written, not rendered; compute values with a [`set`](#set) step |
| `prompts` | no       | Prompts to recognize and answer. See [Prompts](#prompts) |
| `errors`  | no       | Regex patterns for CLI error detection (e.g. `^% .*`). They replace the `$?` exit code check of command lines, except at a `posix` shell prompt. See [Error handling](#error-handling) |
| `fn`      | no       | Named functions under non-empty names, run with `call`. See [Functions](#functions) |
| `attach`  | yes      | How to connect and disconnect. See [Attach](#attach) |
| `script`  | yes      | The steps to run. See [Steps](#steps) |

A key that is repeated in a mapping is an error, not an override. See [Top-level fields](SPEC.md#top-level-fields).

## Prepare

`prepare` is a local shell script that Autobot sources first, e.g. to authenticate. A non-zero exit aborts the run. It reads the declared `env` variables, and what it assigns is their value:

```yaml
env:
  ARTIFACTORY: https://artifactory.example.com
  SONIC_VERSION: "202511"
  SONIC_IMAGE:

prepare: |
  SONIC_IMAGE="$ARTIFACTORY/sonic/$SONIC_VERSION/sonic.swi"
```

- A plain assignment counts, like an `export` (until a `set +a`); a name `env` doesn't declare sets nothing. What the script prints is shown, not read.
- It is not a template, and reads each variable that has a value as `$KEY`. A shebang names its shell, unchecked.

See [`prepare`](SPEC.md#prepare).

## Attach

| Field      | Required | Description |
|------------|----------|-------------|
| `spawn`    | yes      | The command to spawn, e.g. `ssh host` or `telnet host port` |
| `timeout`  | no       | How long to wait for the first output of the spawned command (default 300s) |
| `script`   | no       | Steps to run right after the spawn, before the main `script` |
| `breakout` | no       | Steps to run after the main `script`, to clean up and disconnect |

A run goes in this order:

1. `prepare` runs; `env` and `spawn` are resolved.
2. `spawn` starts, and Autobot waits for its first output.
3. `attach.script` runs.
4. The main `script` runs.
5. `attach.breakout` runs, also when step 3 or 4 failed.
6. The session is closed.

If the spawn fails (step 2), no step runs after it, the breakout included, and nothing undoes `prepare`: one that sets something up, such as a tunnel, must clean up after itself. A breakout error never replaces the script's error. A breakout that fails after a script that completed fails the run with exit status 4. See [`attach`](SPEC.md#attach) and [A breakout that doesn't finish](SPEC.md#a-breakout-that-doesnt-finish).

```yaml
attach:
  spawn: ssh jumphost
  script:
    - line: a dut attach ldp448
    - return: 1
      after: "attached to"
  breakout:
    - control: c
      delay_before: 2s
    - line: logout            # the device
    - control: "]"            # leave the console server, once the device asks for a login again
      after: '[Ll]ogin: ?$'
      timeout: 30s
    - line: logout            # the jump host: this closes the connection
```

### Logging out

Over plain `ssh`, closing the connection ends the login. Behind a console server it doesn't: the console stays logged in for whoever attaches next. Autobot never sends a logout of its own, so the breakout must do it. The breakout above does it in three parts, then leaves the console server and logs out of the jump host:

1. `control: c` clears the line, in case the script failed in the middle of a command. The `delay_before` lets the device read what was sent before, because Ctrl-C discards unread input.
2. `line: logout` logs out. Use `line`, not `cmd`: a `cmd` would wait for a shell prompt, and the login prompt would be answered with the credentials again.
3. The next step waits for the login prompt with `after`. End the pattern with `$`, so that `Last login: ...` in a banner doesn't match, and give the wait a `timeout`. When the logout is the breakout's last step, a block with only a name carries the wait: `- block: {name: logged out}` with the same `after` and `timeout`.

The last `logout` closes the connection, so nothing waits after it.

If the login prompt doesn't come, the breakout fails and the run reports `Session may be left logged in`. A breakout that runs before the login was done types `logout` at the login or password prompt, which the device logs as a failed login; that matters where failed logins lock an account. A run that answered a `sendEach` prompt and finished no breakout after it logs a `>> no logout` warning. See [SPEC.md](SPEC.md#logging-out).

### The environment

The spawned command gets Autobot's own environment, with `TERM=dumb` and `NO_COLOR=1`, and nothing else: the `env` section is for templates only. To give the spawned command a variable, export it before starting Autobot, or set it on the `spawn` line (`spawn: env LC_ALL=C ssh host`). The rendered `spawn` line is printed, so leave a secret in the environment.

See [The environment of the spawned process](SPEC.md#the-environment-of-the-spawned-process). The spawned command runs on a terminal of 500 rows by 80 columns; see [The window of the spawned process](SPEC.md#the-window-of-the-spawned-process).

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

`posix: true` marks a shell prompt as that of a POSIX shell, which has `$?`. For `cmd` it matters only with top-level `errors`: see [Error handling](#error-handling). Next to `send` it is a validation error.

**An interactive prompt** has `send`, and Autobot answers it whenever it appears while a `cmd` waits for a shell prompt. An `after` wait, a `sleep` and a `line` answer nothing. See [Response selection](SPEC.md#response-selection). A string is one fixed answer:

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
| [`set`](#set) | Sets variables in `vars`, with values rendered as templates |

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

After the last line, the step checks the result. With `assert`, the output must match. Otherwise the exit code of the last line is read with `echo $?` and must be 0. With top-level `errors`, the patterns stand in for the exit code check, except at a `posix` shell prompt (see [Error handling](#error-handling)). The exact rules are in [SPEC.md](SPEC.md#cmd--send-commands-to-the-shell).

The output is what the command printed: the prompt, ANSI escape sequences and, where it is recognized, the echo of the command are removed. Text printed without a final newline shares a line with the prompt and is not captured. See [Captured output](SPEC.md#captured-output).

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

**Embedded scripts.** A `cmd` string that starts with `#!` is uploaded to a temp file on the remote, run with the interpreter of its shebang, and removed afterwards, except after some abnormal endings. The remote shell must be POSIX-compatible and have `base64 -d`, `tee` and `wc`. Without `assert`, the script's exit code is always checked, also with `errors`.

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

### `set`

Assigns variables in `vars` where the step stands in the script, so its templates see `env`, `session.*` and what earlier steps registered:

```yaml
- cmd: show version
  register: version_output
- set:
    url: "https://{{ vars.host }}/api"
    image: "{{ env.SITE }}.swi"
    summary: "{{ vars.version_output | trim }}"
    retries: 3                # not a string: stored as the number 3
```

- Every string is rendered, also inside lists and mappings. Numbers, booleans and null are stored as written. A rendered value is text, stored as rendered (not stripped).
- The names are assigned in order, so a later value can read an earlier one: `b: "{{ vars.a }}-y"`.
- If any value fails to render, the step fails and none of its names is assigned.
- An expression that gives a boolean is an error here as anywhere else: write `true` as YAML, or say which text you mean, e.g. `{{ (vars.a == vars.b) | tojson }}`.
- `vars` is global: a `set` in a function or block stays after it.
- Takes `after`, `when`, `delay_before` and `delay_after`, but no `timeout`.

The progress line names the variables, never their values: `>> set: vars.url, vars.image, vars.summary, vars.retries`. See [SPEC.md](SPEC.md#set--set-variables).

## Common step properties

Every step type except `sleep` takes these optional fields:

| Field          | Description |
|----------------|-------------|
| `after`        | Regex to wait for in the output before the step runs. Sets `session.before` and `session.match`. A `cmd` still waits for a shell prompt after the match; to answer something that isn't a shell prompt, use `line` |
| `when`         | Template. The step is skipped if it renders to an empty string, `false`, `0` or `none` (in any letter case) |
| `delay_before` | Duration to wait before the step |
| `delay_after`  | Duration to wait after the step |
| `timeout`      | Limit for each wait and each send of the step (default 300s), not for the step as a whole. Not on `line`, `return` and `set` |

They apply in this order: `after`, `when`, `delay_before`, the step, `delay_after`.

On a `call` or `block`, `timeout` bounds only the `after` wait; the steps inside keep their own. To leave a field at its default, omit the key: an empty value such as `after:` is a validation error. See [SPEC.md](SPEC.md#common-step-properties).

## Durations

A duration is a number of seconds (`5`, `0.5`) or a number with a unit: `500ms`, `30s`, `2m`, `1h`. See [Duration Format](SPEC.md#duration-format).

## Templating

These values are [Jinja2](https://jinja.palletsprojects.com/) templates: `cmd`, `assert`, `line`, `after`, `when`, the values of a [`set`](#set) step, a prompt's `send` string and `attach.spawn`. Other values, such as `prepare`, the `env` defaults, the top-level `vars`, `expect` and `errors`, are used as written. So are the values a `sendEach` takes from `vars`: `password: '{{ env.PW }}'` in `vars.creds` is sent literally.

```yaml
env:
  SWITCH:                                    # required
  BASE_URL: https://artifacts.example.com
  FORCE: false

script:
  - cmd: wget -P /tmp {{ env.BASE_URL }}/sonic-broadcom.swi
  - cmd: show version
    register: version
  - cmd: sonic-installer install -y /tmp/sonic-broadcom.swi
    when: "{{ vars.version | contains('SONiC.4.1') }}"
```

| Variable         | Value |
|------------------|-------|
| `env.*`          | The variables the `env` section declares, and nothing else |
| `vars.*`         | The `vars` section, plus what `register` and `set` stored |
| `session.before` | The text before the last `after` match, or the output of the last command |
| `session.match`  | The text that matched the last `after` pattern or shell prompt |

Two filters are added to Jinja2's own: `contains('text')` and `search('regex')`.

A variable of the `env` section takes its value from the first of `-e KEY=VALUE`, the environment and its default, before `prepare` for the script to read and again after it: `autobot upgrade.autobot.yaml -e SWITCH=sonic -e FORCE=yes`.

- A key with an empty value is required.
- The default gives the type: a string, a boolean (`1`, `true`, `yes`, `on`, `0`, `false`, `no`, `off`) or a number. Any other value is an error.
- A default is a literal, not a template; compose a value in `prepare` or in a [`set`](#set) step.
- `env` has the declared keys only: `{{ env.HOME }}` needs a `HOME` key. A declared name takes whatever the environment has under it, so avoid names the shell already exports (`HOSTNAME`, `USER`, `HOME`).

Things to watch for:

- A key that isn't there is a template error. `{{ vars.x | default('y') }}` gives a fallback.
- `{{`, `{%` and `{#` always start Jinja2 syntax, even in shell code. Bash's `${#arr[@]}` fails to render; wrap it in `{% raw %}...{% endraw %}`.
- An expression that gives a boolean can't be sent or stored as text. Use it in `when` or `{% if %}`, or say which text you mean: `{{ 'on' if vars.debug else 'off' }}`.

See [SPEC.md](SPEC.md#jinja2-templating).

## Error handling

By default, a `cmd` step fails on a non-zero exit code.

**Per step:** `ignore_error: true` logs the failure and goes on.

```yaml
- cmd: show bogus
  ignore_error: true
```

It covers command failures only: a non-zero exit code, a failed `assert`, an `errors` match and an embedded-script upload mismatch. A timeout, a closed connection or a template error always ends the script.

**Global error patterns:** top-level `errors` detects errors by output pattern, for CLIs without exit codes (e.g. Arista EOS). The patterns are matched against each command line's [output](#cmd). They replace the exit code check of command lines, except at a prompt with `posix: true`: give the device's shell prompt its own entry with it, and a `cmd` whose last line ends there gets both checks, the patterns first, then the exit code.

```yaml
prompts:
  - name: bash               # defined first: the cli regex below also matches 'bash-5.1#'
    expect:
      - '^\-?bash(?:\-\d+\.\d+)?[#\$] ?$'
      - '^\[[\w\-\.]+@[\w\-\.]+ [^\]\r\n]*\][#\$] ?$'
    posix: true
  - name: cli
    expect: '^[\w\-\.]+[>#] ?$'   # not 'switch(config)#': see examples/eos-bootstrap.autobot.yaml

errors:
  - '^% .*'

script:
  - cmd: show version        # ends at 'switch#': the patterns only
  - cmd: bash                # ends at '[admin@switch ~]$': the patterns, then $?
  - cmd: test -f /mnt/flash/startup-config
  - cmd: exit                # back at 'switch#': the patterns only
```

- What counts is the prompt the last line of the `cmd` ends at, where `echo $?` is sent, so keep a shell command last in its `cmd`.
- The patterns apply in the shell too, so anchor them: unanchored, `% .*` matches `Use% Mounted on` from `df -h`.
- A `posix` regex must not match the CLI's prompt: the check would be typed into the CLI, and the step would time out. Where the CLI's regex also matches the shell's prompt, define the `posix` entry first: of two regexes that match at the same place, the first one is taken.
- At a `posix` prompt too, `assert` replaces the exit code check, and `ignore_error` covers a non-zero code.

See [SPEC.md](SPEC.md#cmd--send-commands-to-the-shell) for the rest, such as an `exit` from one `posix` shell to another.

## Functions

Define a sequence of steps once in `fn` and run it with `call`. A `call` to a function that isn't defined is a validation error. See [`fn`](SPEC.md#fn).

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

`ctx` gives the plugin the session (`ctx.session`), the run's variables (`ctx.vars`), the `errors` patterns (`ctx.errors`), the steps of a function (`ctx.function(name)`), the config (`ctx.config`), the reporter (`ctx.reporter`), `ctx.render(template)`, `ctx.run_steps(steps)`, `ctx.build_handler(prompt)` and, for a cleanup of its own, `ctx.run_breakout(steps, what)`.

- `ctx.session.sendline(text, timeout=timeout)` sends a command; follow it with `ctx.session.get_prompt(...)`. With `solicit=True` it is a raw send, like a `line` step.
- `ctx.session.sendcontrol(char, timeout=timeout)` sends a control character.
- `ctx.session.posix` is true while the session is at a `posix` shell prompt, and false once anything is sent.
- `ctx.vars[name] = value` stores a value, as `register` and `set` do, for the templates after it. Each run starts with a copy of the script's `vars`; `ctx.config` is the script as loaded, to read.
- `ctx.reporter.say(text)` prints a `>> ` progress line at the step's level, like the built-in steps' lines; `say(text, "warn")` for a failure the step goes on from. Don't write to stderr yourself.
- Raise `autobot.errors.RunError` for what the device did, or `autobot.errors.ScriptError` for a bad value in the script. Most other exceptions from the plugin's own code are reported as a bug in the plugin.

A plugin's key can't be a built-in step key or a common step property name. See [SPEC.md](SPEC.md#common-step-properties).

## Schema and reference

The JSON Schema is in [`schemas/autobot.2026-10.json`](schemas/autobot.2026-10.json). `autobot schema` prints it with a definition for each installed plugin step, and needs no network.

For everything else, see [`SPEC.md`](SPEC.md).
