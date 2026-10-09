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
autobot [run] <script.yaml> [-a KEY=VALUE ...] [--traceback]
autobot validate <script.yaml> [<script.yaml> ...] [-q] [--traceback]
autobot schema [--traceback]
```

`autobot <script.yaml>` is short for `autobot run <script.yaml>`: anything other than `run`, `validate`, `schema`, `-h` or `--help` as the first argument runs a script. To run a script file named `run`, `validate` or `schema`, use `autobot run schema` or `autobot ./schema`.

| Flag                              | Description                                                 |
|-----------------------------------|-------------------------------------------------------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | Pass an argument accessible as `{{ args.KEY }}` in templates. Repeat the flag for more arguments; the value is everything after the first `=`, and the last value given for a key wins. A template that reads an argument that wasn't given fails with `template error: args has no key 'KEY'; pass it with --arg KEY=VALUE` |
| `--traceback`                     | Also print the Python traceback of an error that is reported without one (see [Errors and exit status](#errors-and-exit-status)) |
| `-h`, `--help`                    | Show help (`autobot -h` lists the subcommands, `autobot run -h` the options) |

`autobot schema` prints the JSON schema to stdout, extended with the step types of installed plugins (see [Schema](#schema)).

### Checking a script without running it

`autobot validate` reads each script and validates it as `run` does, and stops there. It renders no template and runs nothing from the script: `attach.prepare` isn't run, nothing is spawned and nothing is sent. Use it in an editor hook or in CI:

```
$ autobot validate upgrade.autobot.yaml login.autobot.yaml
upgrade.autobot.yaml: valid
Validation errors:
  script.0.cmd.timout: Extra inputs are not permitted [extra_forbidden]
login.autobot.yaml: invalid
```

A valid script gets one line, `<path>: valid`. One that isn't gets the error `run` would report for it, in the same words (an unreadable file, a YAML error, or `Validation errors:`), and then `<path>: invalid`. All of it goes to stderr; nothing is written to stdout. The exit status is 0 when every script is valid and 1 when any isn't. `-q` prints nothing for a valid script; the options may come anywhere among the scripts. There is no `--arg`: the result depends only on the file and on the installed plugins, not on arguments or on the environment. Installed plugins are loaded, since their steps are part of what is valid: their import code and their models' validators run, so validate with plugins you trust.

A valid script passes `run`'s validation: `run` won't report `Validation errors`, a YAML error or an unreadable file for it. `run` can still stop before anything is spawned with a `Script error` or a malformed `--arg` (an `env` default that can't be resolved, a missing `--arg` that `env` reads, a prompt `send` with a template syntax error, a `sendEach` collection that can't be resolved), and it can fail during the run: templates are rendered only by `run`, and the device has its own say. See [SPEC.md](SPEC.md#checking-scripts-with-validate) for the exact guarantee.

A run that completes prints `>> run completed` and exits with status 0. If the script can't be loaded, the CLI prints one error on stderr and exits with status 1 before anything runs. That covers an installed plugin that can't be loaded (`Plugin error: ...`: it fails to import, its executor lacks a usable `key`, `model` or `execute` or raises while one of them is read, it uses a reserved key or common-name field, it reuses another plugin's key, or its model is the model of a built-in step; `autobot schema` reports it the same way), a missing or unreadable file, invalid YAML (reported with its line and column), a key repeated in the same mapping (YAML keys must be unique, so a second `script:` is an error, not an override), a validation failure (`Validation errors:`, then one line for each error: `  <location>: <message> [<type>]`, with the offending value where that helps, but never the value of a prompt's `send`, a `line` or an `env` entry), an `--arg` without `=`, a closed stdout (`>&-`; use `> /dev/null` to discard the session's output), a template error or reference cycle in the top-level `env`, a template syntax error in a top-level prompt's `send`, and a top-level `sendEach` collection that can't be resolved. A malformed command line (e.g. `-a` with no value) prints usage and exits with status 2. An error while the script runs is reported after any breakouts have run; see [Errors and exit status](#errors-and-exit-status).

Autobot's own `>> ...` messages and errors go to stderr. The session's output is echoed to stdout, with ANSI escape sequences removed and otherwise exactly as the device sent it: it is never wrapped, cut or styled. So `autobot script.yaml > device.log` keeps the device's transcript, and `2> run.log` keeps what Autobot did. Where both go to the same terminal or file, each message starts on a line of its own, after the device's prompt.

On a terminal Autobot's messages are styled so they stand apart from the device's output: the `>>` marker is blue for a step, green for something completed, yellow for a failure the run goes on from, red for the step that fails (`>> step failed (...): ...`, printed where it fails, before the breakouts), and dim for bookkeeping; an error report starts in red. The steps of a block or of a called function are indented under the line that starts them (`>>   cmd: ...`). The words are the same without the styles, and a pipe or a file gets plain text. Set `NO_COLOR` to turn the styles off on a terminal, or `FORCE_COLOR` to keep them in a pipe. A password is never among them: a `line` step prints `>> line sent`, and an answered prompt `>> prompt answered: <name>`, without the text. The full list of messages is in [SPEC.md](SPEC.md#output).

A run on a terminal looks like this:

```
>> attach: ssh admin@sonic
admin@sonic's password: 
>> prompt answered: login
admin@sonic:~$ 
>> block enter: Upgrade
>>   cmd: show version
show version
SONiC Software Version: SONiC.4.2.0
admin@sonic:~$ 
>> block completed: Upgrade
>> breakout: detaching
>> line sent
>> run completed
```

### Errors and exit status

An error while the script runs is reported after the breakouts have run and the session is closed:

```
Run failed in upgrade.autobot.yaml: timed out after 30.0s waiting for a shell prompt ('sonic')
  at script.3.block.script.1 (cmd: sonic-installer install -y image.swi)
```

The first line says what went wrong, the `at` line where: the step's path in the script (the second step of the block that is the fourth step of `script`) and what it is. A step of a function is at `fn.<name>.script.<i>`, followed by a `called from` line for each `call` that led to it. A failed step, a timeout, a closed connection, a template error, a failed `prepare` and a `spawn` command that isn't found are all reported this way, without a Python traceback.

Ctrl-C stops the run the same way: the breakouts run, the session is closed, and the CLI prints `Interrupted` and the step it stopped in. The process then ends from the interrupt signal itself, so a shell loop that runs `autobot` once per device stops there instead of going on to the next device.

A breakout that fails is reported as well, after whatever else ended the run, and the last line says what it may mean:

```
Breakout failed in upgrade.autobot.yaml: timed out after 30.0s waiting for the after pattern '[Ll]ogin: ?$'
  at attach.breakout.2 (control)
Session may be left logged in: the script completed, but a breakout did not finish; the run sent credentials (prompt 'login')
```

When that is all that went wrong, the exit status is 4, not 0: the script's work is done, but the console may still be logged in (see [Logging out](#logging-out)). After a failed script the status stays 3. A run that answered a `sendEach` prompt and ran no breakout after it logs a warning, `>> no logout: ...`; its exit status is not affected.

**Stopping a run.** Ctrl-C is the way to stop a run and have it log out: `kill -INT <pid>` and `timeout -s INT 10m autobot ...` do the same. Three endings run no breakout at all:

- `SIGTERM` (a plain `kill` or `timeout`, `systemctl stop`, a cancelled CI job) and `SIGHUP` (the terminal or ssh session that runs `autobot` goes away) end the process at once, after one line: `Interrupted (SIGTERM): the session was closed without running the breakouts; the device may be in an unknown state and may still be logged in`.
- Output that can't be written ends the run at once with status 3: `autobot script | head -1`, a log collector that exits, a full disk. The report is `Run failed in ...: cannot write stdout: [Errno 32] Broken pipe; the session was closed without running the breakouts; ...`.

After these a console behind a console server may still be logged in, a command may still be running, and an embedded script's temp file may be left on the device. So don't pipe the output into something that may exit before the run does (redirect it to a file, or use `| tee log`), and run a long job where a closed terminal doesn't reach it: in `tmux` or `screen`, or under `nohup`, which makes the run ignore the hang-up and sends its output to `nohup.out`. See [SPEC.md](SPEC.md#a-run-that-ends-without-its-breakouts).

Anything else is a bug in Autobot or in a plugin. The CLI says which (`Unexpected error in Autobot: ...` or `Unexpected error in plugin '<key>': ...`) and prints the Python traceback to report.

| Status | Meaning |
|--------|---------|
| 0      | The run completed; `autobot validate` found every script valid |
| 1      | The script couldn't be loaded; nothing ran. `autobot validate` found a script invalid |
| 2      | Malformed command line |
| 3      | The run failed, or its output could not be written |
| 4      | The script completed, but a breakout didn't finish: the session may have been left logged in |
| 70     | Unexpected error (a bug in Autobot or a plugin) |
| 130    | Interrupted (Ctrl-C), after the breakouts: the process ends from `SIGINT`, which a shell reports as 130 |
| 143, 129 | Ended by `SIGTERM` or `SIGHUP`, without the breakouts: the process ends from the signal, which a shell reports as 143 or 129 |

`--traceback` also prints the Python traceback of an error that is normally reported without one. The report of an operating-system, encoding or recursion error ends with `(run with --traceback for details)`, since such an error may have more behind it than its message says. See [SPEC.md](SPEC.md#errors-while-the-script-runs) for the full list of errors.

## Script Structure

A script is a YAML file with the following top-level fields:

```yaml
autobot: 2026-10

env:                    # defaults for environment variables (a variable that is set wins)
  IMAGE_URL: https://...

vars:                   # arbitrary data accessible as {{ vars.KEY }}
  creds:
    - username: admin
      password: secret

prompts:                # interactive prompt handlers
  - name: cli
    expect: ['^\w+@[\w.-]+:[^\r\n]*[$#] ?$']
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
    - control: "]"
    - line: q

script:                 # main steps to execute
  - cmd: show version
  - call: check_boot
```

| Field     | Required | Description                                                                                                                  |
|-----------|----------|------------------------------------------------------------------------------------------------------------------------------|
| `autobot` | yes      | Schema version: `2026-10` |
| `env`     | no       | Defaults for environment variables. `{{ env.KEY }}` reads any variable of the environment, declared here or not; a key of this section gives a variable a value when the environment doesn't set it (a value of the environment is used as written, not rendered as a template). Supports nesting in any order: `{{ env.OTHER_KEY }}`; a reference cycle (`env cycle: A -> B -> A`) is a load error |
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
| `prepare`  | no       | Local script to run before spawning (e.g. auth, tunnel setup). A shell script is the run's rc script: the variables it exports are set for the rest of the run (see [`prepare` as an rc script](#prepare-as-an-rc-script)). Leading blank lines, whitespace and a BOM are ignored. Uses the shebang for the interpreter, or `/bin/sh` without one. Aborts on non-zero exit, or if the interpreter can't run (`prepare script could not run ('#!...'): ...`) |
| `spawn`    | yes      | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`). Must name a command, as written or once rendered: not empty or blank, and not just quotes or a backslash (`''`) |
| `timeout`  | no       | Timeout for the initial spawn                                                                                                       |
| `script`   | no       | Steps to run immediately after spawn (before main script)                                                                           |
| `breakout` | no       | Steps to run in `finally` after the main script (cleanup/disconnect); see [Logging out](#logging-out)                               |

### Lifecycle

1. `attach.prepare` runs locally (if defined) — aborts on failure. The variables it exports are applied, and a `spawn` that reads `env` is rendered after it
2. `pexpect.spawn(attach.spawn)` — waits up to `attach.timeout` (default 300s) for initial output. The output is left unconsumed, so a login or shell prompt that arrives with the banner is handled by the first prompt wait.
3. `attach.script` steps execute (e.g. jump-host commands)
4. Main `script` steps execute
5. `attach.breakout` steps execute (best-effort, errors logged to stderr)
6. Session closed

The session is always closed, even if the initial spawn wait times out or the breakout fails. A breakout error never replaces an error raised by the script; the original error is what propagates. The same goes for a session that can't be closed (a process that survives being killed): it is logged as `>> close error (...)` next to the script's error, and is the run's error only when nothing else failed. A breakout that fails after a script that completed fails the run, with exit status 4 (see [Logging out](#logging-out)).

If the initial spawn wait fails, nothing after it runs, including `attach.breakout`: no step has sent anything for the breakout to undo. That covers `attach.timeout` expiring before any output (`timed out after <timeout>s waiting for the first output from '<spawn>' (attach.timeout)`), the process exiting before any output (`connection closed before any output from '<spawn>' (exit status <n>)`, or `(killed by <SIGNAL>)`), and a spawn command that isn't found (`The command was not found or was not executable: <command>`). The process is killed and its pty closed, which drops a silent `ssh` or `telnet` connection. `attach.prepare` has already run, and nothing undoes it. A process that prints a banner and then exits has passed the spawn wait: the first step fails (`connection closed while waiting for a shell prompt (...)`), and the breakout runs (its errors are logged).

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
    - control: c
      delay_before: 2s
    - line: logout
    - control: "]"
      after: '[Ll]ogin: ?$'
      timeout: 30s
    - line: logout
```

### Logging out

Over plain `ssh`, closing the connection ends the login. Behind a console server it doesn't: if autobot detaches without logging out, the device's console stays logged in for whoever attaches next. So a breakout that follows a login does three things: it clears the line, logs out, and waits for the login prompt.

```yaml
  breakout:
    - control: c          # drop a half-typed line, stop a command that is still running
      delay_before: 2s    # Ctrl-C also discards what the device has not read yet: let it read first
    - line: logout        # the device's logout command
    - control: "]"        # leave the console server, once the output ends with a login prompt
      after: '[Ll]ogin: ?$'
      timeout: 30s
    - line: logout        # the jump host: this closes the connection, so nothing is waited for after it
```

- Start with the control character: after a failure the device may be in the middle of a command or have half a line typed, and `logout` would go there.
- This breakout takes about 2.5 seconds on every run (the `delay_before` and the half second a line waits after a control character), plus the time the device takes to show its login prompt.
- Give that control character a `delay_before`: a terminal throws away the input it has not handed on yet when Ctrl-C arrives, so a line sent just before it (the `line: exit` of a block's breakout, say) would be lost without a trace.
- Send the logout with `line`, not `cmd`: a `cmd` waits for the next shell prompt, and a login prompt would be answered with the credentials again.
- `after` waits before its step, so the step after the `logout` line carries the wait for the login prompt. If no step follows, use a block with nothing but a name: `- block: {name: logged out}` with the same `after`. Give the wait a `timeout`; the default is 300s.
- Wait for the prompt at the end of the output, not for the word: `'[Ll]ogin: ?$'` is `login:` or `Login:` with nothing after it. The bare word is also in `Last login: ...` and may be in a command's output, and would let the breakout go on with the console still logged in.

If the login prompt doesn't come, the breakout fails at that step and the run fails with it: the CLI reports `Breakout failed in ...` and `Session may be left logged in: ...`, and exits with status 4 when the script itself completed (3 when it had failed already). A run that answered a `sendEach` prompt and after that finished no breakout at all, neither a block's nor `attach.breakout`, logs a warning: `>> no logout: the run sent credentials (prompt 'login'), and no breakout ran after them to log out before the session is closed`. When the connection had already closed, the last line says that instead (`Connection closed before a breakout finished: a console behind a console server may still be logged in`). Autobot never sends a logout of its own. A breakout also runs when the run was stopped before the login was done; its `logout` is then typed at the login or password prompt, which the device logs as a failed login. See [SPEC.md](SPEC.md#logging-out).

### `prepare` as an rc script

A `prepare` script that a shell runs works like an rc file: the variables it exports are set for the rest of the run. Templates read them through `env`, and the spawned command (`ssh`, `telnet`, ...) inherits them.

```yaml
attach:
  prepare: |
    . ~/.config/lab/credentials.sh
    export JUMP_HOST=$(lab-inventory jump-host)
  spawn: ssh -J {{ env.JUMP_HOST }} admin@{{ args.host }}
```

- A script without a shebang is sourced by `/bin/sh`. One whose shebang names `sh`, `bash`, `dash`, `ksh` or `zsh` (`#!/bin/bash`, `#!/usr/bin/env bash`) is sourced by that shell. The shebang may carry `set` options (`#!/bin/sh -eu`) or the end-of-options `-` (`#!/bin/sh -`). A script for any other interpreter (`#!/usr/bin/env python3`), or for a shell with any other argument (`#!/bin/bash -r`), is executed as a program and sets nothing: a process can't change its parent's environment.
- Autobot takes what the script changed: an exported variable with a new value is set, one that is gone is unset. `_`, `SHLVL`, `PWD` and `OLDPWD` are never taken, and a `cd` in the script doesn't move Autobot.
- Precedence, lowest to highest: a default of the `env` section, the environment Autobot was started with, Autobot's `TERM=dumb` and `NO_COLOR=1`, a variable `prepare` set. The `env` defaults are rendered again after `prepare`, so a default may reference a variable that only `prepare` sets.
- `prepare` is itself a template and sees `env` as it is before the script runs. A `spawn` that reads `env` is rendered after it.
- The spawned process gets, each overriding the one before: Autobot's own environment, `TERM=dumb` and `NO_COLOR=1`, and what `prepare` changed. The `spawn` command is looked up in the `PATH` of that environment. `prepare` itself runs with `TERM=dumb` and `NO_COLOR=1`, and `{{ env.TERM }}` reads the same, so a `prepare` that exports or unsets `TERM` or `NO_COLOR` decides it. The `env` section's defaults are for templates and aren't passed on. To give the spawned command a variable, export it in `prepare`, or set it on the `spawn` line: `spawn: env LC_ALL=C ssh host`, or with a template, `spawn: env "SITE={{ args.site }}" ssh host`. The rendered line is split into words, so quote a templated value; one that may contain `"` or `\` belongs in `prepare`. The rendered line is also printed, so a secret interpolated there shows in `>> attach:`. A local shell inherits `PROMPT_COMMAND`, `BASH_ENV`, `ENV` and `PS1`, and `ssh` forwards `LANG` and `LC_*`; unset what is in the way in `prepare`, or, for a process that inherits nothing, spawn it through `env -i`: `spawn: env -i PATH=/usr/bin:/bin ssh host`.
- A script that exits non-zero aborts the run and sets nothing. The script's output stays its own, and Autobot prints only how many variables were set and unset, never a name or a value. A value that a template puts into `spawn` or a command is printed with it (`>> attach: ...`, `>> cmd: ...`), so leave a secret in the environment for the process to inherit rather than interpolating it. The environment is read back through a temp file that has no name, so no file with its values is left behind, however Autobot ends.
- A script that sets its own `EXIT` trap and then calls `exit`, or that ends with `exec`, ends its shell before the environment can be read: it sets nothing, and a warning says so (`>> prepare: environment not read: ...`). End such a script at the end of the file or with `return`. The same warning, with its own reason, is printed when the environment can't be read for another cause, e.g. an exported value too large to start a command with. The run goes on in each case.

The spawned process runs on a terminal of 500 rows of 80 columns, whatever terminal Autobot itself runs in. The width is a standard terminal's; the height lets a long command fit on the screen of a line editor that wraps (a `TERM` other than `dumb`), up to 40,000 characters with its prompt, so its echo is recognized. `ssh` and `telnet` pass the size on, so a remote pager stops once per 500 lines where it would stop once per 24, and `more` shows no `--More--` for shorter output. That is no reason to leave a pager on: turn it off as on any terminal (`terminal length 0`, `--no-pager`). A device behind a console server never sees this size and goes by its own width and length. For another height, set it before the program starts: `spawn: sh -c 'stty rows 24 && exec ssh host'`. See [SPEC.md](SPEC.md#the-window-of-the-spawned-process).

## Prompts

Prompts define how autobot recognizes and responds to interactive patterns in the session output.

A prompt with `return: true` (or no `send` field) is a **shell prompt** — when matched, autobot knows the previous command finished and the next one can be sent. A `return: true` prompt can't have `send` (it would never be sent); the script fails validation with `return_with_send`:

```yaml
- name: cli
  expect:
    - '^\w+@[\w.-]+:[^\r\n]*[$#] ?$'
    - '^(arista-)?bmc-boot=> ?$'
  return: true
```

Prompt regexes are searched for anywhere in the unread output, with `re.DOTALL` (`.` also matches line breaks) and without `re.MULTILINE` (`$` matches only at the end of the output read so far). Start a regex with `^`, as above, to anchor the prompt to the start of its line: the line breaks, escape sequences and any stray `\r`, NUL or BEL before a prompt are consumed first, so the prompt is the first thing in the unread output. Without it, output that only contains something like a prompt and ends a read with the prompt character (`scp admin@host:/x $`) is taken for one. The cost is a prompt with other text in front of it on its line, such as one printed right after output with no final newline: it isn't recognized. End a shell prompt regex at the prompt character, as above. A regex that stops short (e.g. `[^\$]+`, which stops before the `$`) leaves the rest of the prompt in the stream: it becomes the start of the next command's output, so the echo isn't removed and `register`, `assert` and `errors` see `$ <command>`. One that ends in `.+` swallows whatever follows the prompt. For a colored prompt, see [ANSI escape sequences](SPEC.md#ansi-escape-sequences) before anchoring with `$`.

A prompt with `send` is an **interactive prompt**: autobot responds automatically. `expect` is a regex or a non-empty list of regexes. The regexes are alternatives, so any of them triggers the prompt. An empty regex (`''`, here or in a `fields` entry's `match`) is a validation error, because it would match at once, before any output. The `send` field accepts two forms.

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
        - match: ['[Ll]ogin: ?$', 'Username: ?$']
          field: username
        - match: '[Pp]assword: ?$'
          field: password
```

End a login regex with `$`: the prompt is the last thing the device has written. Without it, the `Last login: ...` line that follows a login is taken for a second login prompt and answered with the next user name. If a slow console delivers that line in pieces, put the host name in the regex (`'switch1 login: ?$'`); see [SPEC.md](SPEC.md#sendeach).

This resolves `vars.creds`, and each item is one login attempt (credential cycling). Each entry sends its field of the current item when one of its regexes matches. When the same entry matches again (e.g. `login:` after a rejected password, or `Password:` twice), autobot moves on to the next item. A password-only login such as `ssh admin@host` works with the same prompt. If autobot must move on and no item is left, the step fails with `responses exhausted`. The prompt is then still waiting for an answer, so autobot presses no Return at it on its own (that would be an empty user name or password) until the script sends something. See [SPEC.md](SPEC.md#response-selection) for the exact rules.

Without `fields`, each item (a string or number) is sent as it is, in answer to any of the prompt's `expect` regexes. A boolean is never sent: an unquoted `true`, `yes` or `on` as an item or a field's value is an error that tells you to quote it (`'true'`, `'yes'`):

```yaml
- name: pin
  expect: ['PIN:']
  send:
    each: vars.pins
```

`each` must be a path of keys under `vars` (e.g. `vars.creds` or `vars.site.creds`) that leads to a list. With `fields`, every item must be a mapping with each entry's field; without it, every item must be a string or number. A path or item that doesn't fit stops the script: before anything runs for the top-level prompts, or on entering the block for a block's prompts. For example: `prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'`. See [SPEC.md](SPEC.md#sendeach).

## Step Types

### `cmd` — Send command(s) to the shell

Waits for a prompt, sends the command, waits for the next prompt, and checks the result.

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
```

`cmd` accepts a string or list of strings. Each line waits for a prompt before sending. A multiline string is rendered as a template first, then split on newlines (`\n`, `\r\n` or `\r`, and nothing else; blank lines are skipped), so a `{% for %}` loop may span lines and send one command per iteration. `cmd: []` sends nothing and waits for no prompt; an empty string (`cmd: ""`) sends one empty line.

After each command line, the step waits for a shell prompt and, if top-level `errors` patterns are defined, checks that line's output against them — raising (and sending no further lines) on a match.

After the last command line, the step:
1. If `assert` is defined, checks the captured output of all lines for a matching pattern — raises if none match
2. Otherwise, if no top-level `errors` are defined, checks the return code of the last line via `echo $?` — raises on non-zero

An `assert` or `errors` pattern can't be empty (an empty regex matches any output), and `register` can't be an empty name; both fail validation. An `assert` that renders to an empty or invalid regex aborts the step.

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

- The terminal echo of the sent line is removed, also when the device's line editor wrapped a long line at the margin of its terminal (a line break, `\r`, a blank and a backspace, NUL padding): the echo is recognized by what a terminal shows of it. If the output doesn't start with the echo of the sent line (e.g. echo disabled with `stty -echo`), it is left as is. See [The echo of a sent line](SPEC.md#the-echo-of-a-sent-line).
- Line breaks are preserved as `\n`.
- ANSI escape sequences (colors, cursor movement) are removed, so `assert`, `errors` and `register` see plain text. `after` and prompt `expect` regexes, on the other hand, match the raw output, escape sequences included; see [ANSI escape sequences](SPEC.md#ansi-escape-sequences).
- The prompt line is excluded. Because of that, any text printed without a trailing newline (it shares a line with the next prompt) is not captured — e.g. `printf 'x\ny'` captures `x`.

**Important:** `cmd` blocks until a prompt appears after the command. For commands that won't return a prompt (e.g. `reboot`, `exit`), use `line` instead. The same goes for a command that shows nothing until Return is pressed, such as connecting to an idle console (`consutil connect 0`): autobot presses Return for a console that stays silent for 5 seconds, but never while a `cmd` is running, so that `cmd` would time out. Send it with `line`; the next `cmd` waits for the prompt and presses Return if needed.

**Very long lines.** A line is sent as it is, however long. Sending has the step's `timeout`, like every wait: if the far side stops reading, the step fails with `timed out after <timeout>s while sending a line (<sent> of <total> bytes sent)`. The part that was sent stays on the far side's input line, typed and not entered. Until a control character has been sent, Autobot presses no Return and sends no line there, since either would enter the cut line (`line not sent: part of a line ...`). So a breakout that may follow such a step should start with `control: c`, which drops the line at a shell and at most CLIs; one that starts with a `line` or a `cmd` fails at that step, and the session is closed. On a terminal that wraps (a `TERM` other than `dumb`), the output of a line is captured as long as the line fits on the screen of the line editor, 40,000 characters with its prompt. A shell without a line editor (`dash`, `bash --noediting`, a `read`) leaves the line to its terminal, which keeps only so much of it. On Linux that is the first 4095 bytes: the rest is dropped without an error, and the command runs cut off. On macOS, by its documentation (nothing was run there), it is 1023 bytes, and the rest is dropped together with the line break, so the command never runs and the step times out. Where that terminal is the one Autobot spawned the process on, such a line is not sent, and the step fails with `line of <N> bytes not sent: the terminal reads whole lines (canonical mode) and takes 4095 bytes of one, so the last <M> would be dropped without an error` (the limit is the one of the platform Autobot runs on; where it isn't known, nothing is refused). Behind `ssh`, `telnet` or a console server Autobot can't see the terminal that counts: it sends the line and warns once per run (`>> long line: ...`). All of this was measured on Linux only. See [SPEC.md](SPEC.md#the-length-of-a-sent-line).

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

The script is uploaded base64-encoded, in lines of at most 512 base64 characters, to `/tmp/_autobot_<random>` (mode `700`, created under `umask 077`). The remote shell must be POSIX-compatible and provide `base64 -d`, `tee`, and `wc`. The upload is checked by comparing the decoded byte count to the script's length. The temp file is always removed, even if the upload or the script fails. If the step times out while the script or the upload is still running, Autobot first sends a single Ctrl-C to interrupt it, waits for the prompt, and then removes the files; the timeout still aborts the script, even with `ignore_error`. A script that ignores `SIGINT` can't be interrupted, so its files are left behind. Removal is best-effort: each prompt wait takes at most 10 seconds (or the step timeout, if that's shorter), and a failed cleanup is logged without replacing the step's own error.

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
4. `breakout` steps execute in `finally` (best-effort, errors logged to stderr)
5. If `prompts` was defined, restore the previous session handlers

When the session is sitting at a shell prompt, the swap and the restore keep it there if the new prompts recognize that prompt, so the next `cmd` sends at once: no 5s idle wait and no extra newline. If they don't, the next `cmd` waits for one of the new prompts, and an idle shell never prints one. So enter a sub-CLI whose prompt the block's prompts expect with `line`, not `cmd`, and leave it in the breakout (e.g. `line: exit`). See [SPEC.md](SPEC.md#prompt-state-across-a-swap).

Steps 4 and 5 run even if `enter` fails, and step 5 runs even if the breakout fails. A breakout error never replaces an error raised by `enter` or `script`. The script goes on after a block whose breakout failed, but the run then ends with exit status 4, like a run whose `attach.breakout` failed: the block may have left what it entered.

```yaml
- block:
    name: Install SONiC
    script:
      - cmd: sonic-installer install -y image.swi
```

With enter and breakout (the console is entered with `line`: a `cmd` would wait for a prompt that an idle console shows only after Return is pressed):

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

Each value is one character: a letter (either case) or one of ``@ ` [ { \ | ] } ^ ~ _ ?``. Anything else (`""`, `"ab"`, `"1"`) fails validation with `control_char` when the script is loaded.

Ctrl-C, Ctrl-\\ and Ctrl-Z make the far side's terminal throw away the input it holds, which includes a line that was sent just before and not read yet. Give such a `control` an `after` or a `delay_before` when it follows a `line` (see [Logging out](#logging-out)). The other way round autobot waits by itself: a line is sent half a second after a control character at the earliest, because a shell that is still handling the interrupt would drop the start of it.

## Common Step Properties

All step types except `sleep` support these optional fields:

| Field          | Description                                                    |
|----------------|----------------------------------------------------------------|
| `after`        | Regex pattern — wait for this to appear in output before executing. On match, populates `session.before` and `session.match`. Must not be empty, as written or once rendered; omit the key to skip the wait |
| `when`         | Jinja2 conditional — step is skipped if the rendered result, stripped and lowercased, is `""`, `false`, `0` or `none` (so `False`, `None` and `" FALSE "` also skip) |
| `delay_before` | Duration to wait before the step                               |
| `delay_after`  | Duration to wait after the step                                |
| `timeout`      | Bounds each wait and each send of this step (default 300s), not the step as a whole |

`line` and `return` steps do not support `timeout`.

`after` doesn't replace a `cmd`'s wait for a prompt: the step waits for the pattern, then for a prompt, and sends the command there, so an `after` that matches while something is still running doesn't send the command into it. That wait never presses Return, since a Return could answer a question or reach a running command; if no shell prompt comes within the step's `timeout`, the step fails. An `after` that ends at the shell prompt itself is fine: the command is sent at once. `line`, `return` and `control` send as soon as the pattern matches, so use `line` to answer something that isn't a shell prompt.

For `cmd`, `timeout` applies separately to each wait and each send: `after`, each prompt wait, the sending of each line, the `$?` check and the embedded-script upload. For `control` it bounds the `after` wait and the sending of each character. For `call` and `block` it bounds only the `after` wait: the steps inside a function or block keep their own `timeout` (default 300s) and don't inherit it.

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

Durations must be finite and can't be negative (`.nan`, `.inf` and `-.inf` are rejected), and `true`/`false` and `null` (e.g. a bare `sleep:`) aren't durations.

## Templating

These values are [Jinja2](https://jinja.palletsprojects.com/) templates: `cmd` (including embedded scripts), `assert`, `line`, `after`, `when`, prompt `send` strings (rendered each time one is sent, so they can use `vars` registered by earlier steps; not the values `sendEach` reads from `vars`), `attach.spawn`, `attach.prepare`, and `env` values. Other values, such as `expect` and `errors`, are used as written.

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
| `env.*`          | The whole environment (`{{ env.HOME }}`), with `TERM=dumb` and `NO_COLOR=1` and the variables `attach.prepare` exported, plus the `env` section's defaults for the variables that aren't set; a variable set nowhere is undefined, so `{{ env.X \| default('y') }}` works |
| `vars.*`         | `vars` section (also populated at runtime by `cmd` steps with `register`) |
| `args.*`         | CLI `--arg` flags                       |
| `session.before` | Text before the last `after` match, or the captured output of the last command (see [What counts as output](#what-counts-as-output)) |
| `session.match`  | Text that matched the last `after` pattern or shell prompt |

A key wins over a mapping method of the same name: after `register: values`, `{{ vars.values }}` is the registered output. `vars.items()` and `vars.get('k', 'default')` work as long as no key is named `items` or `get`.

A boolean is not text. An expression that gives one, such as `{{ vars.debug }}` with `debug: true`, `{{ a == b }}` or `{{ value | contains('x') }}`, is a template error wherever the result is sent or stored, because no single spelling (`true`, `True`, `yes`) is right for every device. Quote the value in the script (`debug: 'true'`) if it is meant as text, or say in the template which text you mean: `{{ vars.debug | string }}` (`True`), `{{ vars.debug | tojson }}` (`true`) or `{{ 'on' if vars.debug else 'off' }}`. Conditions are unaffected: `when`, `{% if %}` and comparisons inside an expression work on the value. A null still renders as `None`.

An expression that fails while a template is rendered, such as `{{ 1/0 }}`, is a template error like a syntax error or an undefined variable: `template error: ZeroDivisionError: division by zero`.

## Error Handling

By default, `cmd` steps check the return code via `echo $?` and raise on non-zero. You can change this behavior in two ways:

**Per-step:** Set `ignore_error: true` to log and continue:

```yaml
- cmd: show bogus
  ignore_error: true
```

`ignore_error` covers command failures only: a non-zero exit code, a failed `assert`, an `errors` match, and an embedded-script upload mismatch. Timeouts, a closed connection (`connection closed while waiting for ...`, naming the prompts, `after` pattern or `$?` check it was waiting for, or `connection closed while sending a line`), template errors and prompt-response failures (`responses exhausted`) always abort the script.

**Global error patterns:** Define top-level `errors` to detect errors by output pattern instead of exit code. This is useful for CLIs that don't use standard exit codes (e.g. Arista EOS):

```yaml
errors:
  - '% .*'

script:
  - cmd: show bogus    # raises because output matches '% .*'
```

Patterns are searched with `re.MULTILINE` (`^` and `$` match at each line, and `.` doesn't cross line breaks) against the captured output once the prompt returns. The echoed command itself is never matched, so a comment like `! note` in a command doesn't trigger `'! .*'`.

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

## Plugins

A plugin adds a step type. It is an executor class registered in the `autobot.steps` entry-point group, with a `key` (the step's YAML key), a `model` (a pydantic model of the step's own fields) and `execute(step, ctx, timeout)`. The model can't be a built-in step's model or `PluginStep`; a subclass of one is fine, and two plugins may share a model, since a plugin step is dispatched by its key. `ctx` gives it the session (`ctx.session`), the config, `ctx.render(...)`, `ctx.run_steps(...)` and, for a cleanup of its own that must not stop what comes after it, `ctx.run_breakout(steps, what)`: an error in those steps is logged as `>> <what> error (...)` and fails the run as a failed breakout does.

`ctx.render(template)` renders text that is sent or stored, where an expression that gives a boolean is a template error (see [Templating](#templating)). A plugin that renders a condition, to read the result as a yes or no, calls `ctx.render(template, condition=True)`, as the runner does for `when`. The rules for keys and model fields are in [SPEC.md](SPEC.md#common-step-properties).

When a plugin sends text itself, it tells the session what kind of send it is:

- `ctx.session.sendline(text)` sends a command. The following `ctx.session.get_prompt(...)` waits for the command's prompt and never presses Return while it waits, however long the command is silent.
- `ctx.session.sendline(text, solicit=True)` is a raw send, like a `line` step: the next prompt wait presses Return once if nothing shows within 5 seconds. Use it for text that leaves the session at an idle console, such as a connect command.
- `ctx.session.sendcontrol(char)` sends a control character, like a `control` step.

Each send takes a `timeout` in seconds, by keyword: `ctx.session.sendline(text, timeout=timeout)`, `ctx.session.sendcontrol("c", timeout=timeout)`. It is 300 by default; pass on the `timeout` that `execute` was given, as the built-in steps do. A send that isn't complete in that time, because the far side has stopped reading, raises `TimeoutError` (`timed out after <timeout>s while sending a line ...`); see [SPEC.md](SPEC.md#the-length-of-a-sent-line).

A plugin reports a failure the user can act on by raising `autobot.types.RunError` (what the device did; `autobot.steps.StepFailure` is one) or `autobot.types.ScriptError` (a bad value in the script). The CLI reports these as a failed run with the step's path, and likewise a `TimeoutError`, an `EOFError` or a pexpect error, whether the session raises it or the plugin does. Any other exception that the plugin's own code raises is reported as a bug in the plugin, with its traceback. That includes an `OSError`, `UnicodeError` or `RecursionError` of the plugin's own; the same error from Autobot's session underneath, e.g. a write to a pty that is gone, is a failed run (see [Errors and exit status](#errors-and-exit-status)).

## Schema

The full JSON Schema is in [`schemas/autobot.2026-10.json`](schemas/autobot.2026-10.json). `autobot schema` prints it, with a definition added for each installed plugin step: a step with that plugin's key is checked against the plugin's model and the common step properties. A step has at most one plugin key, and a plugin can't use a built-in step key or a common step property name, as its key or as a field of its model, its key can't be `plugin` (the generated definition would take the name of the `pluginStep` catch-all), and two installed plugins can't share a key (see SPEC.md, "Common Step Properties"). The schema ships inside the package, so an installed autobot prints the schema of its own version and needs no network. It is normative: autobot accepts the scripts the schema accepts. The exception is step keys: the static schema accepts any unknown step key as a possible plugin step (it still checks the step's common properties, such as `timeout` and `when`), while autobot rejects a key that no installed plugin provides. Autobot also compiles the regexes when it loads the script, which a JSON schema can't do: one that doesn't compile in `errors`, a prompt's `expect` or `match`, or an `assert` or `after` without template syntax, fails validation with `invalid_regex` before anything runs.

For the detailed specification, see [`SPEC.md`](SPEC.md).
