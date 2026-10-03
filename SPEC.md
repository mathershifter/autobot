# Autobot — Console Robot Script Engine

Autobot executes YAML-defined scripts against remote consoles via pexpect. It handles login negotiation, multi-hop session management, and cleanup automatically.

## Architecture

```
YAML script → pydantic validation → Runner → Session (pexpect) → remote console
```

- **YAML script** defines environment, credentials, prompt patterns, functions, attach config, and a step-based script.
- **Pydantic models** (`Config` and subtypes) validate and parse the YAML against `schemas/autobot.2026-10.json`. They also check what a JSON schema can't express: every `call` target is defined in `fn`, and every plugin step's fields are valid for its plugin model. All of this happens when the script is loaded, before `attach.prepare` runs or anything is spawned.
- **Runner** renders Jinja2 templates, dispatches steps, and manages block breakouts.
- **Session** wraps pexpect, handles prompt detection and credential cycling.

## YAML Script Structure

All fields validated by pydantic against `schemas/autobot.2026-10.json`. The JSON schema is normative: the models reject every document the schema rejects, and accept what it accepts except where a static schema can't decide. One such case is step keys. A static schema can't know which plugins are installed, so its `pluginStep` accepts any object that has no built-in step key and whose [common step properties](#common-step-properties) are valid (it applies `$defs.stepCommon`); the step's other keys aren't checked. The models accept a step key that isn't built in only if an installed plugin registers it, and otherwise reject the step (`invalid_step`). They also reject a step with more than one plugin key, which the static schema accepts as a `pluginStep` (see [Common Step Properties](#common-step-properties)).

An optional field is either omitted or given a value of its type. An explicit `null`, including a key with an empty YAML value (`after:`, `timeout: ~`), is invalid for every optional field, including the common step properties of plugin steps: omit the key instead to get the default. The error type is `null_value`, located at the key. A plugin's own fields follow the plugin's model.

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `autobot` | yes | Schema version: exactly `2026-10`. Surrounding whitespace or a trailing newline is rejected. Any other value is a validation error (`unsupported_version`) at `autobot`: `unsupported autobot version '<value>'; expected 2026-10`, or for `2026-08`, `autobot 2026-08 is no longer supported; use 2026-10 (see "Migrating from 2026-08" in SPEC.md)` (see [Migrating from 2026-08](#migrating-from-2026-08)). |
| `env` | no | String key-value defaults, overridden by OS environment variables. Supports nesting: a value may reference other keys (e.g. `{{ env.OTHER_KEY }}`, `{{ env['OTHER_KEY'] }}`), in any order. Each default is rendered once, after the values it references; the text it renders to isn't rendered again. Any read of a key's value counts as a reference, including `env.get('KEY')` and `env.items()`. An OS variable's value is used exactly as written: it isn't a template and is never rendered, so `{{` in it is plain text, and a key that references it gets that text. The default it overrides isn't used, so the default's references aren't followed. A reference cycle, a value that references itself directly or through other keys, is an error naming the cycle (`env cycle: A -> B -> A`), and so is a chain more than 50 keys deep. A reference to a key that isn't in `env` is an undefined variable (`template error: env has no key 'KEY'`), so `default` applies to it. Accessible as `{{ env.KEY }}` |
| `vars` | no | Arbitrary objects, accessible as `{{ vars.KEY }}` |
| `prompts` | no | Named prompt/response definitions for interactive sessions |
| `errors` | no | Regex patterns for CLI error detection (e.g. `% .*`). When defined, replaces `$?` exit code checking. |
| `fn` | no | Named functions (reusable step sequences) |
| `attach` | yes | Session spawn and lifecycle config |
| `script` | yes | Ordered list of steps to execute |

### `prompts`

Each prompt has:
- `name` — identifier
- `expect` — a regex, or a non-empty list of regexes, to match against session output. The regexes are alternatives: any of them triggers the prompt. A single regex is the same as a list holding it. Every entry of the list is a single regex: a list inside `expect` (the grouped entry of earlier versions) is a validation error (`grouped_expect`, see below). `expect: []` would never fire, and an empty regex (`''`) matches at once, before any output, so both are validation errors (see below). Required, except in a prompt whose `send` is a `sendEach` with `fields`: there `expect` must be absent, because the patterns come from the `fields` entries (see [`sendEach`](#sendeach)).
- `return` — optional boolean; if `true`, matching this prompt means "we have a shell prompt" and the pending `cmd` is sent. Defaults to `false`. A prompt with no `send` field is also treated as a shell prompt. A prompt with `return: true` must not have `send`, in either form (a string, including `send: ''`, or a `sendEach` with or without `fields`): it would never be sent. That is a validation error (`return_with_send`) at `prompts.N.send`: `a return prompt is a shell prompt and sends nothing; remove send or return`. It is reported together with any other error of the prompt, e.g. `expect_with_fields`, a missing `expect` or `grouped_expect`.
- `send` — optional; what to send when a pattern matches. Accepts two forms:
  - A string, for a *simple prompt*: a single question with a single answer, such as a confirmation. The string is sent on any match: whichever of the prompt's `expect` regexes matches, each time the prompt appears (see [Response selection](#response-selection)). `send: ''` sends an empty line, i.e. presses Enter.
  - A `sendEach` object for data-driven responses, such as a login that asks for a user name and then a password (see below).

  A simple prompt:
  ```yaml
  - name: confirm
    expect:
      - 'continue\?'
      - 'are you sure\?'
    send: 'yes'
  ```

  `send` must be a YAML string. Quote answers that YAML reads as something else: unquoted `yes`, `no`, `on`, `off`, `true` and `false` are booleans, and `1234` is a number. A literal sequence of answers (a list of strings, or a list of lists) is not a `send` form: a login or any other sequence of prompts uses a `sendEach` with `fields`, with the values in `vars`.

  The `send` string is a Jinja2 template. It is rendered each time it is sent, so it sees `vars` registered by earlier steps and `session.*` as last set by an `after` or a shell prompt (the prompt match that triggers the response doesn't set them). A syntax error is reported when the prompts are loaded: before `attach.prepare` runs for the top-level `prompts`, and on entering the block for a block's `prompts`. An undefined variable is reported when the response is sent, prefixed with `prompt '<name>': `, and aborts the step waiting for the prompt. Both are [template errors](#jinja2-templating).

These are validation errors at the prompt, reported like any other before anything runs. Each message ends with `(see "Migrating from 2026-08" in SPEC.md)` except `send_type`'s:

| Rule broken | Location | Type and message |
|-------------|----------|------------------|
| `send` is a list (of strings, of lists, or empty) | `prompts.N.send` | `send_list`: `send is a single string: for a simple prompt write send: '<response>'; to answer a sequence of prompts such as a login, use sendEach with fields and keep the values in vars` |
| `send` is a boolean or a number (e.g. unquoted `send: yes`) | `prompts.N.send` | `send_type`: `send must be a string; quote it, e.g. send: 'yes' or send: '1234' (unquoted, YAML reads yes, no, on, off, true, false and numbers as booleans or numbers)` |
| an `expect` entry is a list | `prompts.N.expect.M` | `grouped_expect`: `each expect entry is a single regex, and the regexes are alternatives; to answer a sequence of prompts such as a login, use sendEach with fields` |
| `expect: []` | `prompts.N.expect` | `too_short`: `expect must be a regex or a non-empty list of regexes` |
| `expect: ''`, or an `expect` entry `''` | `prompts.N.expect`, or `prompts.N.expect.M` for an entry | `string_too_short`: `a regex must not be empty: an empty regex matches at once, before any output` |

The `too_short` and `string_too_short` messages don't end with the migration hint. In a prompt whose `sendEach` has `fields`, an empty `expect` (`[]`, `''` or `['']`) is reported only as `expect_with_fields` (see [`sendEach`](#sendeach)): there `expect` must be absent. The empty-`expect` errors are reported together with the prompt's other errors, such as `return_with_send` or `grouped_expect`.

An invalid `send` (`send_list`, `send_type`) is reported on its own: the prompt's other rules, such as `return_with_send`, `grouped_expect`, a missing or empty `expect`, are checked only once `send` is valid. So a 2026-08 login with a grouped `expect` and a `send` list reports `send_list` first. The JSON schema rejects the same documents. A block's prompts are validated the same way, at `script.N.block.block.prompts.M...`.

#### `sendEach`

Iterates over a collection from `vars` to build responses. With `fields`, each entry pairs the prompt it answers with the item field it sends:
```yaml
- name: login
  send:
    each: vars.creds
    fields:
      - match: ['(?:L|l)ogin:', 'Username:']
        field: username
      - match: '(?:P|p)assword:'
        field: password
```

`fields` is a non-empty list of entries. Each entry has exactly two keys:
- `match` — a regex, or a non-empty list of regexes. No regex may be empty (`''`): it would match at once, before any output. The regexes of one list are alternatives for the same prompt: each of them sends the entry's field.
- `field` — the key of the item to send. It is a single mapping key; a `.` in it is part of the key, not a path.

A prompt whose `sendEach` has `fields` has no `expect`: its patterns are the `match` regexes of the entries, in order. So each field is sent only in answer to its own prompt, and the number of values sent per item always matches the prompts that ask for them.

Without `fields`, each item is converted to a string and sent in answer to any of the prompt's `expect` patterns, e.g. a PIN list:
```yaml
- name: pin
  expect: ['PIN:', 'Passcode:']
  send:
    each: vars.pins
```
Here `expect` is required. Its regexes are alternatives, as in every prompt: each item answers whichever of them matches. Use `fields` to answer several different prompts.

These rules are validation errors, reported like any other before anything runs:

| Rule broken | Location | Type |
|-------------|----------|------|
| `fields: []` | `prompts.N.send.fields` | `too_short` |
| `match: []` | `prompts.N.send.fields.K.match` | `too_short` (`match must be a regex or a non-empty list of regexes`) |
| `match: ''`, or a `match` entry `''` | `prompts.N.send.fields.K.match`, or `prompts.N.send.fields.K.match.M` for an entry | `string_too_short` (`a regex must not be empty: an empty regex matches at once, before any output`) |
| a `fields` entry without `match` or `field`, or with another key | `prompts.N.send.fields.K.<key>` | `missing` / `extra_forbidden` |
| a `fields` entry that is a string (the 2026-08 form) | `prompts.N.send.fields.K` | `fields_entry` (`since 2026-10 a fields entry pairs a regex with a field: write {match: <regex>, field: <name>} (see "Migrating from 2026-08" in SPEC.md)`) |
| `expect` next to `fields` (even `expect: []` or `expect: ''`) | `prompts.N.expect` | `expect_with_fields` (`a prompt whose sendEach has fields has no expect: the patterns are the fields' match regexes`) |
| no `expect`, and no `sendEach` with `fields` | `prompts.N.expect` | `missing` |
| `return: true` with a `sendEach` (with or without `fields`), or any other `send` | `prompts.N.send` | `return_with_send` (`a return prompt is a shell prompt and sends nothing; remove send or return`) |

The JSON schema states all of them, so the models and the schema reject the same documents. A block's prompts are validated the same way, at `script.N.block.block.prompts.M...` (the step's type tag comes before its key).

In the example, `vars.creds` is resolved, and each item becomes one credential set: the value of each entry's field, in entry order. The collection is resolved when the prompts are loaded (at script start for the top-level `prompts`, on entering the block for a block's `prompts`, so a block sees `vars` registered by earlier steps), and its values are sent as they are, never rendered as templates.

`each` is a path under `vars`: `vars` followed by one or more `.`-separated keys (e.g. `vars.creds`, `vars.site.creds`). Each key names a key of a mapping; list indexes, attributes, `env`, `args` and `session` can't be used. Any other form is a validation error.

The collection must be a list. Without `fields`, each item must be a string, number or boolean. With `fields`, each item must be a mapping with the `field` of every entry, and each field's value must be a string, number or boolean. An empty list is allowed; the prompt then has no credential sets (see [Response selection](#response-selection)).

A collection that doesn't meet these rules is an error when the prompts are loaded: before `attach.prepare` runs for the top-level `prompts` (reported by the CLI as a `Script error`), and on entering the block, before its `enter` steps, for a block's `prompts` (the run stops; the block's `breakout` doesn't run, and `attach.breakout` does). The message is `prompt '<name>': sendEach '<each>': <problem>`, where `<problem>` is one of:

| Problem | Example |
|---------|---------|
| A key is missing | `no key 'nope' in 'vars.site'` |
| A value on the path isn't a mapping | `'vars.site' is a string, not a mapping` |
| The collection isn't a list | `'vars.creds' is a mapping, not a list` |
| An item isn't a mapping (with `fields`) | `item 2 is a string, not a mapping` |
| An item lacks a field | `item 2 has no field 'password'` |
| A field's value isn't a string, number or boolean | `item 2 field 'password' is null, not a string, number or boolean` |
| An item isn't a string, number or boolean (without `fields`) | `item 0 is a mapping; without fields each item must be a string, number or boolean` |

Items are counted from 0. Types are named as in YAML: `a mapping`, `a list`, `a string`, `a number`, `a boolean`, `null`.

#### Response selection

A **simple prompt** (a `send` string) has no state: it sends its string, rendered at that moment, on any match, whichever of its `expect` regexes matches, each time the prompt appears. It is never exhausted, so a question asked again (e.g. `continue?` once per file) is answered again. A device that keeps asking because the answer is wrong is stopped only by the timeout of the step waiting for the prompt.

A **`sendEach`** is a list of *credential sets*, each an ordered list of responses:

| `sendEach` form | Credential sets |
|-----------------|-----------------|
| with `fields` | One set per item: the value of each entry's `field`, in entry order |
| without `fields` | One set per item, holding the item converted to a string |

Responses come from the current set, starting with the first. When a pattern of the prompt matches:
- **With `fields`:** a regex of entry k (the first entry is 0) sends item k of the current set, the entry's field. The regexes of one entry are the same prompt.
- **Without `fields`:** every `expect` regex sends the set's one item, the current item.

Before sending, the current set advances to the next set when:
- with `fields`, the matching entry's item was already sent from the current set (the sequence starts over, e.g. `login:` matches again after `Password:`, or `Password:` matches twice), or
- without `fields`, the current item was already sent (any later match of any of the prompt's regexes).

A `sendEach` prompt raises `RuntimeError`:
- `prompt '<name>': responses exhausted` when the set must advance and there is no next set.
- `prompt '<name>': no response available` when the collection is empty, so the prompt has no credential sets.

The selection starts over at the first set, with nothing sent, on every prompt wait (each `get_prompt`).

With `fields` entries `login:` → `username` and `Password:` → `password` over `vars.creds: [{username: admin, password: pw1}, {username: admin, password: pw2}]`:

| Device prompts (a rejected login restarts the sequence) | Sent |
|---|---|
| `login:`, `Password:`, `login:`, `Password:` | `admin`, `pw1`, `admin`, `pw2` |
| `Password:`, `login:`, `Password:`, `login:` | `pw1`, `admin`, `pw2`, `admin` |
| `Password:`, `Password:` (e.g. `ssh admin@host`) | `pw1`, `pw2` |
| `login:`, `Password:`, `login:`, `Password:`, `login:` | `admin`, `pw1`, `admin`, `pw2`, then `responses exhausted` |

A password-only login (e.g. `ssh admin@host`) works with the same prompt: the `username` entry just never matches.

### `fn`

Named functions callable from `call` steps. Each function contains a `script` array of steps (required; may be empty):
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
| `prepare` | no | Local script to run before spawning the session (e.g. authentication, tunnel setup). Rendered as a Jinja2 template first (see [Jinja2 Templating](#jinja2-templating)). Leading spaces, tabs, line breaks, a byte order mark (U+FEFF) and the zero-width characters U+200B and U+2060 are removed from the rendered script; what remains is written to a temp file. The script is written as UTF-8, whatever the locale's encoding. If it starts with `#!`, the script is executed directly and the shebang picks the interpreter (a `\r` ending the shebang line is dropped). Otherwise it runs under `/bin/sh`, and the progress line says so: `>> prepare: running local script (no shebang, using /bin/sh)`. Aborts if the script exits non-zero (`prepare script failed with exit code <N>`). If the script can't be started (e.g. the interpreter is missing or not executable), aborts with `prepare script could not run ('<first line>'): [Errno <n>] <reason>`, chained from the `OSError`. The temp file is removed in every case, and nothing is spawned after a failure. |
| `spawn` | yes | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`) |
| `timeout` | no | Timeout for the initial spawn (duration) |
| `env` | no | Environment variables for the spawned process. Replaces the full process environment (not merged with the parent). If omitted, defaults to `TERM=dumb` and `NO_COLOR=1`. An empty map (`env: {}`) is not omitted: the process gets an empty environment. An empty value (`env:`) is invalid, like any explicit `null`. The `spawn` command is looked up in the `PATH` of `env`, or in the system default path (`/bin:/usr/bin` on Linux) when `env` has no `PATH`, as with the default; give a full path or set `PATH` for commands elsewhere. |
| `script` | no | Steps to run immediately after spawn (before main script) |
| `breakout` | no | Steps to run in `finally` after the main script (cleanup/disconnect) |

The attach lifecycle:
1. `attach.prepare` runs locally (if defined) — aborts on failure
2. `pexpect.spawn(attach.spawn)` — waits up to `attach.timeout` (default 300s) for initial output. The output is left unconsumed, so a login or shell prompt that arrives with the banner is handled by the first prompt wait.
3. `attach.script` steps execute (e.g. jump-host commands)
4. Main `script` steps execute
5. `attach.breakout.script` executes (best-effort, errors logged to stderr)
6. Session closed

Steps 5 and 6 run after step 3 or 4 fails, and step 6 runs even if the breakout fails. A breakout error never replaces an error raised by `attach.script` or `script`.

If the spawn wait (step 2) fails, the run stops there. Neither `attach.script` nor `script` runs, and **`attach.breakout` doesn't run**: the breakout undoes what the steps did on the remote (log out, leave a console server session), and no step has run or sent anything. The spawned process, if any, is closed: its pty is closed, and a process that is still running is terminated (`SIGHUP` and `SIGINT`, then `SIGKILL` if it ignores them). Closing the process is what frees the line; a silent `ssh` or `telnet` that is killed drops its connection. Then the error propagates (the CLI prints a traceback and exits with status 1). The spawn wait fails when:
- `attach.timeout` expires before any output: `TimeoutError` (`timed out after <timeout>s waiting for the first output from '<spawn>' (attach.timeout)`, with the rendered `spawn` command). The process is still running until it is closed.
- the process exits before any output: `EOFError` (`connection closed before any output from '<spawn>'`, with the rendered `spawn` command). Once the process is closed, its exit status or the signal that ended it is appended: ` (exit status <n>)` or ` (killed by <SIGNAL>)`, e.g. ` (killed by SIGKILL)`. `SIGHUP` isn't reported, because closing the pty sends it as well; a process ended by it gets no suffix.
- the `spawn` command isn't found or isn't executable: pexpect's `ExceptionPexpect` (`The command was not found or was not executable: <command>`). No process is started.
- the operator interrupts the run (`KeyboardInterrupt`) during the wait

`attach.prepare` has already run when the spawn wait fails. Nothing undoes it; a `prepare` that sets something up (e.g. a tunnel) must clean up after itself.

Any output ends the spawn wait successfully, even if the process then exits. A process that prints a banner and exits before a prompt fails in the first step that waits on it (usually with `EOFError`), and `attach.breakout` runs as it does after any step failure. Its steps that wait on the session fail with `EOFError`; the first one ends the breakout and is logged (e.g. `>> breakout error (EOFError): connection closed while waiting for a shell prompt ('sh')`), and the original error propagates.

## Step Types

### `cmd` — Send command(s) to the shell

Waits for a prompt, sends the command, waits for the next prompt, and checks the result.

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
```

`cmd` accepts a string or list of strings. A string is rendered as a Jinja2 template as a whole, then split into lines on newlines, and blank lines of the result are skipped. So a Jinja2 block (e.g. `{% for %}`...`{% endfor %}`) may span lines, and a template that expands to several lines sends each of them. A string that renders to nothing but blank lines sends one empty line (like `cmd: ""`). Each item of a list is rendered on its own and split the same way, and the lines of all items are sent in order; a Jinja2 block can't span items. The whole `cmd` is rendered once, after the step's first prompt wait and before its first line is sent, so a template error sends nothing and no line sees the output of an earlier line of the same step. Each line waits for a prompt before sending.

After each command line, the step waits for a shell prompt. If top-level `errors` patterns are defined, that line's captured output is checked against them; on a match the step raises and no further lines are sent.

After the last command line, the step:
1. If `assert` is defined, checks the captured output of all lines for a matching pattern — raises if none match
2. If `assert` is not defined and no `errors` are defined, checks the return code of the last line via `echo $?` — raises on non-zero

When `assert` is defined, it replaces the return code check — the assertion pattern is the success criteria. If top-level `errors` patterns are defined, they replace the `$?` check.

#### Captured output

The captured output of a command line is the text the session prints between sending the line and the next shell prompt, with:
- The terminal echo of the sent command removed. The echo is matched ignoring whitespace and `\r`, across wrapped lines, and in readline's horizontal-scroll form (`\r<` + visible tail) used for long lines. If the echo doesn't match (e.g. echo disabled), the output is left unchanged.
- Line endings normalized to `\n`.
- ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)).
- The trailing partial line before the prompt match (the prompt prefix) dropped. Output that doesn't end with a newline is therefore not captured.

`errors` patterns are matched with `re.MULTILINE` against the captured output after the shell prompt returns, so they never match the echoed command, and the session is left at the prompt when the error is raised. An error that is printed without a prompt returning results in a timeout rather than an error match.

#### ANSI escape sequences

The session removes ANSI escape sequences that match `types.ANSI_ESCAPE_RE`: CSI sequences (`ESC [`, parameter and intermediate bytes, a final byte; e.g. colors, `ESC [?2004h`, cursor movement) and two-byte `ESC` sequences whose second byte is `@` through `Z` or `\` through `_`. Other sequences are left in the text, e.g. `ESC ( B`, `ESC =`, `ESC 7`, the 8-bit CSI byte `0x9B`, and the body of an OSC sequence such as a terminal title (only its `ESC ]` is removed). `attach.env` defaults to `TERM=dumb` and `NO_COLOR=1`, so many programs print no color to begin with.

Stripped text, which never contains a removed sequence:
- The captured output of a command (see above), and so the text `assert` and `errors` patterns are matched against, the value `register` stores, and `session.before` after a shell prompt.
- The session output echoed to the operator. Everything read from the session is written to stdout with the sequences removed (`session.CleanWriter`), while autobot's own `>> ...` messages and errors go to stderr.

Raw text, with escape sequences as received:
- `after` patterns are matched against the raw output stream, and the `session.before` and `session.match` set by an `after` match are raw: escape sequences, `\r\n` line endings and the command echo are all kept. A pattern such as `AABBCC` doesn't match output printed as `AA ESC[1m BB ESC[0m CC`; match around the sequence instead (e.g. `AA.*CC`).
- Prompt `expect` regexes (and the `match` regexes of `sendEach` `fields` entries) are matched against the raw stream too, but while it waits for a prompt the engine also consumes each line break and each escape sequence as it arrives (see [Prompt Handling](#prompt-handling-get_prompt)). A prompt regex should therefore match text that comes after the prompt's last escape sequence and before any that follows it. For a prompt printed as `ESC[32m PS1> ESC[0m`, `PS1> ` matches, but `PS1> $` doesn't when the `ESC[0m` arrives together with the prompt, as it usually does: it is still in the stream after `PS1> ` when the regex is tried. A regex that spans an escape sequence or a line break may or may not match, depending on how the output arrives. `session.match` after a shell prompt is the text the prompt regex matched.
- The `$?` check's `__AUTOBOT_RC=` marker is also matched against the raw stream.

Set `ignore_error: true` to continue when the command fails:

```yaml
- cmd: show bogus
  ignore_error: true
```

`ignore_error` covers command failures only. These are:
- a non-zero exit code from the `$?` check
- an `assert` where no pattern matches
- an `errors` pattern match (no further lines of the step are sent)
- an embedded-script upload whose byte count doesn't match

An ignored failure is logged (`>> error ignored: ...`), and the script continues with the step's `delay_after` and then the next step. Each of these failures is detected with the session at a shell prompt, so the next step starts from a prompt as usual.

Every other error aborts the step, and with it the script, even with `ignore_error: true`:
- timeouts: waiting for a prompt, for the `after` pattern, or for the `$?` result. The `TimeoutError` messages are `timed out after <timeout>s waiting for a shell prompt ('<name>', ...)` (the names of the current shell prompts, or `none defined`), `... waiting for the after pattern '<pattern>'` (the rendered pattern) and `... waiting for the exit code of the command (echo $?)`
- a closed connection (`EOFError`). The message starts with `connection closed` and names what was being waited for, as the timeouts do: `connection closed while waiting for a shell prompt ('<name>', ...)`, `... while waiting for the after pattern '<pattern>'` and `... while waiting for the exit code of the command (echo $?)`
- template errors (`template error: ...`, see [Jinja2 Templating](#jinja2-templating)), including in a prompt `send` response, and invalid regular expressions in `assert` or `errors`
- prompt-response failures (`responses exhausted`, `no response available`)

#### Capturing output with `register`

Set `register` to store the command's captured output into `vars.<name>`, making it available to subsequent steps via Jinja2 templates as `{{ vars.<name> }}`. The stored value is the output text with leading and trailing whitespace stripped.

```yaml
- cmd: show version
  register: version_output

- cmd: "echo 'Version was: {{ vars.version_output }}'"
```

`register` works with all `cmd` forms: plain commands, command lists, multiline strings, and embedded scripts. The output is captured regardless of whether `assert`, `ignore_error`, or `errors` are in use — as long as execution continues past the step (i.e., the error is either absent or ignored).

For command lists and multiline strings, the captured output of every line is concatenated. When an error is ignored, `register` stores:
- after an `errors` match, the output of the lines up to and including the failing line
- after an exit code or `assert` failure, the output of all lines
- for an embedded script, the script's output, or `""` if the upload failed

When a step aborts (an unignored or unignorable error), `vars.<name>` is left unchanged.

#### Embedded scripts

If `cmd` is a string starting with `#!` (before rendering), it is treated as an embedded script. The shebang line determines the interpreter. The script is written to a temp file on the remote, made executable, executed, and cleaned up automatically.

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
- The script and its `.b64` staging file are always removed: on success, on script failure, on upload failure and on timeout (as far as the interrupt below allows).
- After success or a command failure (exit code, `assert`, `errors` match, upload mismatch) the session is already at a shell prompt, and cleanup just sends `rm -f <file> <file>.b64` and waits for the prompt.
- After any other error, the session may not be at a prompt: a timeout (while the script runs, during an upload line, or in the `$?` check) leaves that command in the foreground. Cleanup then first sends a single Ctrl-C (`^C`, logged as `>> script: interrupt sent: ^C`) with no newline, waits for the shell prompt, and then sends the `rm`. The same applies to a closed connection, a prompt-response failure, or an operator interrupt.
- The interrupt and the cleanup never change the step's outcome: the original error propagates and aborts the script, also with `ignore_error: true` (timeouts are never ignorable). Cleanup doesn't change `session.before` or `session.match`.
- If cleanup succeeds, the session is left at a shell prompt with nothing running, so breakouts start from a prompt. If the interrupt doesn't bring a prompt back (the script ignores `SIGINT`, or the remote doesn't treat `^C` as an interrupt), the prompt wait times out, `rm` isn't sent and the files are left on the remote. The session is then in an unknown state, and breakouts run against whatever is in the foreground.
- Cleanup is best-effort: each of its two prompt waits (before and after the `rm`) takes at most 10s, or the step timeout if that's shorter. Its errors are logged but never replace the step's error.

### `sleep` — Pause execution

```yaml
- sleep: 60s
```

The session output is read during the sleep. If the connection closes, the sleep ends at once with `EOFError` (`connection closed while waiting for the end of a sleep`). The same applies to `delay_before` and `delay_after`.

### `call` — Invoke a named function

```yaml
- call: is_system_running
```

The target must be a function defined in `fn`. This is checked when the script is loaded, for every `call` in the script: the top-level `script`, `attach.script`, `attach.breakout`, every `fn` body, and the `enter`, `script` and `breakout` of nested blocks. That includes a `call` that `when` would skip or that never runs. An undefined target is a validation error of type `undefined_function`, located at the step's path (e.g. `script.3.block.script.0.call`). Functions may call other functions, including themselves; the check doesn't follow calls, so it doesn't detect cycles.

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

#### Prompt state across a swap

The swap (step 1) and the restore (step 5) neither read from the session nor send anything. They change only whether the session counts as being at a shell prompt, that is, whether the next prompt wait (e.g. the first wait of a `cmd`) returns at once (see [Prompt Handling](#prompt-handling-get_prompt)):

- If the session is at a shell prompt, the text of that prompt is checked against the new prompts the way a prompt wait would read it. The text is the prompt's line as the last prompt wait matched it: from the end of the last line break or escape sequence before the match, up to the end of the match, escape sequences in the match included. As in a prompt wait, the match that starts earliest in that text wins, among `\r\n`, escape sequences and the new prompts' regexes, with ties broken the same way. A winning line break or escape sequence is skipped and the rest of the text is checked again. If the winner belongs to a shell prompt, the session stays at the prompt: the next `cmd` sends at once, without the 5-second idle wait or a solicit newline. If it belongs to a prompt with `send`, or nothing matches, the session no longer counts as at a prompt. So does a new prompt regex that isn't valid; the next prompt wait reports the error.
- If the session isn't at a shell prompt (e.g. after a `line`, `return` or `control` step, or a step that timed out), it stays that way. A swap or restore never puts the session at a prompt.

So a block whose prompts recognize the prompt on screen, e.g. one that only adds a handler for a confirmation question, or nests a block with the same shell prompt, costs no extra wait or newline on entry or exit. Captured output is unaffected: the check consumes no output, and `session.before` and `session.match` keep the values of the last prompt wait.

When the session doesn't count as at a prompt, the next prompt wait reads new output with the new prompts, and sends its one solicit newline if nothing matches by its first poll timeout (5 seconds, or the wait's timeout if that is shorter). A prompt that the active prompts don't recognize is never used to send a command, and an idle shell answers the solicit newline with the same prompt again. So:
- A `cmd` as the first step of a block whose prompts don't recognize the current prompt times out. Enter the sub-CLI with `line` (it sends without waiting for a prompt), as in the example below, or also list the current prompt in the block's prompts.
- On exit, the restore runs after the breakout. If the block leaves the session at a prompt that the restored prompts don't recognize (e.g. it has no breakout to leave the sub-CLI), the next `cmd` outside the block times out. A breakout that ends with `line: exit` leaves the session not at a prompt, and the next `cmd` waits for the outer prompt that the exit brings back.

A block breakout's handler reset (step 4) only restarts the response selection; it doesn't change the prompt state. Neither does the attach breakout's. A block without `prompts` doesn't swap, so its prompt state is never checked.

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
        send:
          each: vars.creds
          fields:
            - match: 'login:'
              field: username
            - match: 'Password:'
              field: password
    enter:
      # line: the block's prompts don't recognize the outer prompt, so a cmd would time out
      - line: consutil connect 0
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

The value is required and must be an integer ≥ 1.

### `control` — Send control character(s)

```yaml
- control: "]"           # Ctrl+]
- control: [a, x]        # Ctrl+A then Ctrl+X
```

## Common Step Properties

All step types except `sleep` support:

| Field | Description |
|-------|-------------|
| `after` | Expect regex — wait for this pattern before executing. Matched against the raw output, escape sequences included (see [ANSI escape sequences](#ansi-escape-sequences)). On match, populates `session.before` and `session.match` |
| `when` | Jinja2 conditional — template is rendered, step is skipped if the result is falsy (see below) |
| `delay_before` | Duration to wait before the step |
| `delay_after` | Duration to wait after the step |
| `timeout` | Duration that bounds each wait of this step (default 300s); see below |

`line` and `return` steps do not support `timeout`.

`timeout` bounds each wait of the step separately; it isn't a deadline for the step as a whole, and it doesn't bound `delay_before` or `delay_after`. It applies to:
- `after`: the wait for the pattern, in every step type that has `after`. In `line` and `return`, which have no `timeout`, this wait uses the 300s default.
- `cmd`: also the wait for a prompt before the first line and after each line, the `$?` check, each wait of an embedded-script upload, and (capped at 10s) its cleanup.
- `call` and `block`: only the `after` wait. The steps of the function, or the block's `enter`, `script` and `breakout`, use their own `timeout`, or the 300s default; they don't inherit the `call` or `block` step's `timeout`.
- `control`: only the `after` wait.
- A plugin step: the `after` wait, and whatever the plugin does with the `timeout` it is passed.

`attach.timeout` bounds only the wait for the spawned process's first output; steps don't inherit it either.

These properties also apply to plugin steps. They are handled by the runner; the plugin's own model receives only its plugin-specific fields. Those fields are validated against the plugin's model when the script is loaded, wherever the step appears (the same places as for `call`). A failure is a validation error at the step's path, e.g. `script.0.ech0` with type `extra_forbidden` for a misspelled field.

A plugin step has exactly one plugin key. A step with keys of two or more registered plugins is a validation error of type `invalid_step` at the step's path, e.g. `script.0` with the message `step has more than one plugin key: free, loose` (the keys in the step's order), even when the first plugin's model allows extra fields. A built-in key takes precedence: in `{cmd: x, free: y}` the step is a `cmd` step and `free` is an unknown field (`extra_forbidden`).

Because the runner takes the common step properties out of every plugin step, a plugin can't use their names, and neither can it use a built-in step's key. A plugin is rejected when it is registered (at discovery: at the start of `autobot run` and `autobot schema`, and otherwise when a script needs a plugin or the `Runner` is created) if its key is a built-in step key (`cmd`, `sleep`, `call`, `block`, `line`, `return`, `control`) or a common step property name, or if its model has a field that a script could set through a common step property name: the field's name, its `alias`, or any of its `validation_alias` names. The error is a `PluginError` (from `autobot.registry`, a subclass of `TypeError`) naming the plugin's class, with the distribution and entry point of a discovered plugin, its key and the fields, e.g. `plugin mypkg.TreeExecutor (distribution mypkg, entry point 'tree'), step key 'tree': model TreeStep reuses common step property names, which the runner handles and never passes to the plugin: timeout (field 'limit'), when`, or `plugin mypkg.CmdExecutor (distribution mypkg, entry point 'cmd'): step key 'cmd' is reserved (a built-in step or a common step property)`. A plugin registered in code, rather than discovered, is named by its class alone, e.g. `plugin mypkg.CmdExecutor: step key 'cmd' is reserved (a built-in step or a common step property)`. Like a plugin that fails to import, the CLI reports it as `Plugin error: <message>` on stderr, without a traceback, and exits with status 1 before `attach.prepare` runs or anything is spawned (see [CLI](#cli)).

A plugin is also rejected at registration, before these checks, if its executor doesn't have the shape of a `StepExecutor` (`autobot.protocols`): a `key` attribute that is a non-empty string, a `model` attribute that is a pydantic model class (a `pydantic.BaseModel` subclass), and a callable `execute`. The error is a `PluginError` naming the plugin like the errors above and the first problem found, checked in that order, e.g. `plugin mypkg.BadExecutor (distribution mypkg, entry point 'bad'): executor has no 'model' attribute (a pydantic model class)`. The other messages are `executor has no 'key' attribute (a non-empty string)`, `executor's 'key' must be a non-empty string, got <repr>`, `executor's 'model' must be a pydantic model class (a pydantic.BaseModel subclass), got <repr>`, `executor has no 'execute' method` and `executor's 'execute' must be callable, got <repr>`. The registry is left as it was. `execute`'s signature isn't checked.

A step key belongs to one plugin. A plugin whose key is already registered by another plugin is rejected the same way, at registration, so a step is never dispatched to whichever plugin happened to be discovered last. The error is a `PluginError` naming both plugins, the new one first, with the distribution and entry point of each discovered one, e.g. `plugin pkg_b.EchoExecutor (distribution pkg-b, entry point 'echo'): step key 'echo' is already registered by plugin pkg_a.EchoExecutor (distribution pkg-a, entry point 'echo')`. The registry is left as it was. The CLI reports it like any other plugin error. Registering the same plugin again is not an error: the same executor instance is ignored, and another instance of the same class with the same model replaces it.

A plugin whose entry point raises while it is loaded (its module fails to import, the named attribute is missing, or creating the executor fails) is also a `PluginError`, `entry point '<name>' (distribution <dist>) failed to load: <type>: <message>` (just `<type>` when the exception has no message), chained from the original exception. `KeyboardInterrupt` and `SystemExit` are not wrapped. Discovery stops at the first plugin that fails; a broken plugin is never skipped, because a script or schema without it would fail or validate in a misleading way. The CLI catches a `PluginError` only from discovery: one raised while a script runs is printed as a traceback like any other run-time error.

Order of evaluation: `after` (wait) -> `when` (decide) -> `delay_before` -> execute -> `delay_after`.

### Conditional execution with `when`

The `when` field accepts a Jinja2 template string. The rendered result is evaluated as a boolean gate: it is falsy if, with surrounding whitespace removed and lowercased, it is `""`, `"false"`, `"0"` or `"none"`. A falsy result skips the step entirely. So `False`, `FALSE`, `None` (how Jinja2 renders a null value, e.g. `{{ vars.v }}` when `v` is `null`) and `" false "` are all falsy. Any other result, including `"no"` and `"off"`, runs the step.

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

A bare number must be ≥ 0; a boolean is not a duration, and neither is `null` (so `sleep:` with no value is rejected). A string must be a non-negative number immediately followed by one of the units `ms`, `s`, `m`, `h`, with nothing else (`"5"`, `"1 s"` and `"-1s"` are rejected).

## Jinja2 Templating

These values are rendered as Jinja2 templates:
- `cmd` (a string, or each item of a list, as a whole before it is split into lines; an embedded script as a whole), `assert`, `line`, `after` and `when`
- a prompt's `send` string, each time it is sent (the values `sendEach` takes from `vars` are sent as they are)
- `attach.spawn` and `attach.prepare`
- `env` values (see [Top-level fields](#top-level-fields))

Other values are used verbatim, e.g. `expect`, `errors`, `control`, `call`, `register` and `attach.env`. A plugin step decides which of its own fields it renders.

Any template error in any templated value, a syntax error or an undefined variable, is reported as a `ValueError` with the message `template error: ...`. This includes plugin fields rendered through the runner. It always aborts the step, and with it the script, even with `ignore_error: true`. In a breakout it ends that breakout and is logged, like any other breakout error.

Because these values are templates, `{{`, `{%` and `{#` in them always start Jinja2 syntax, even inside shell code. For example, bash's array length `${#arr[@]}` contains `{#`, which opens a Jinja2 comment, so rendering fails with a template syntax error. To pass such text through literally, wrap it in `{% raw %}...{% endraw %}`, or emit the delimiter from an expression (`{{ '{#' }}`):

```yaml
attach:
  prepare: |
    #!/bin/bash
    arr=(a b c)
    {% raw %}echo "${#arr[@]}"{% endraw %}
```

Available context:

| Variable | Source |
|----------|--------|
| `env` | `env` section of the YAML |
| `vars` | `vars` section of the YAML (also populated at runtime by `cmd` steps with `register`) |
| `args` | CLI `--arg KEY=VALUE` arguments |
| `session.before` | Text captured before the last `after` match (pexpect `before`), or the captured output of the last command when a shell prompt is reached (empty if it printed nothing). The `$?` check and embedded-script cleanup don't change it. |
| `session.match` | Text that matched the last `after` pattern (pexpect `after`), or the text the prompt regex matched when a shell prompt is reached. After an `after` match, `session.before` and `session.match` are raw text; after a shell prompt they have ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)). |

Built-in global: `range`. Use Jinja2 filters for other operations (e.g. `{{ items | length }}`).

### Custom Filters

| Filter | Usage | Description |
|--------|-------|-------------|
| `contains` | `{{ value \| contains('substring') }}` | Returns `True` if `substring` is found in `value` |
| `search` | `{{ value \| search('regex') }}` | Returns `True` if the regex pattern matches anywhere in `value` |

## Prompt Handling (`get_prompt`)

`get_prompt()` only detects and navigates to a shell prompt — it does not send commands. The caller is responsible for sending the command via `sendline()` after `get_prompt()` returns.

The session is *at a shell prompt* from the moment a prompt wait ends at one until anything is sent (a command line, the `$?` check, `line`, `return`, `control`). A prompt wait while the session is at a shell prompt returns at once, without reading output, sending anything, or changing `session.before` and `session.match`. A block's prompt swap and restore can end this state but never start it (see [Prompt state across a swap](#prompt-state-across-a-swap)).

While it waits, the engine also consumes each `\r\n` and each ANSI escape sequence as it arrives: the text before it goes to the captured output, and the escape sequence itself is dropped (see [ANSI escape sequences](#ansi-escape-sequences)). When several patterns match, the one that starts earliest in the unread output wins; on a tie, `\r\n` comes first, then an escape sequence, then the prompts in the order they are defined.

The prompt engine polls the session output in 5-second intervals:
1. If a prompt with no `send` (or `return: true`) matches → return (shell prompt reached)
2. If a prompt with `send` values matches → send the response chosen as described in [Response selection](#response-selection) and continue waiting
3. On 5-second timeout with no match → send a single empty newline to solicit a prompt (once only, and only if no handler has been activated yet)
4. On overall timeout → raise `TimeoutError` (`timed out after <timeout>s waiting for a shell prompt ('<name>', ...)`, naming the current prompts that are shell prompts, or `none defined`)
5. If the connection closes (the process exits) → raise `EOFError` at once (`connection closed while waiting for a shell prompt ('<name>', ...)`, with the same names)

This handles idle consoles that need a return press to display a prompt.

## Migrating from 2026-08

Version `2026-10` changes prompts:
- A `sendEach` with `fields`: each field is now an entry that names the prompt it answers, and the prompt has no `expect`.
- `send` is a single string (a simple prompt) or a `sendEach`. The flat-list and list-of-lists forms are removed.
- Every `expect` entry is a single regex, and the regexes are alternatives. Grouped entries (a list inside `expect`) are removed. `expect` may also be a single regex.
- `send` on a `return: true` prompt is rejected (see below).

To migrate:
1. Change `autobot: 2026-08` to `autobot: 2026-10`.
2. In every prompt whose `send` has `fields` (top-level and block `prompts`), move the patterns from `expect` into the `fields` entries: the pattern that asks for a field becomes that entry's `match`, then remove `expect`.
3. In every prompt whose `send` is a list:
   - If it answers a single question (one response, sent whenever any `expect` pattern matches), write that response as a string: `send: ['yes']` becomes `send: 'yes'`. Quote it if YAML would read it as a boolean or a number.
   - If it answers a sequence of prompts, such as a user name and then a password, or tries several credential sets, move the values into `vars` and use a `sendEach` with `fields`: one entry per prompt of the sequence, its pattern as the entry's `match`. Remove `expect`.
4. Replace every grouped `expect` entry: in a prompt that answers a sequence, its patterns become `fields` entries (step 3); in any other prompt, list its patterns as separate entries.

```yaml
# 2026-08
- name: login
  expect:
    - ['(?:L|l)ogin:', '(?:P|p)assword:']
  send:
    each: vars.creds
    fields: [username, password]

# 2026-10
- name: login
  send:
    each: vars.creds
    fields:
      - match: '(?:L|l)ogin:'
        field: username
      - match: '(?:P|p)assword:'
        field: password
```

Several patterns for the same field become one entry with a `match` list.

A literal login moves its values into `vars`:

```yaml
# 2026-08
prompts:
  - name: login
    expect:
      - ['login:', 'Password:']
    send: [[admin, pw1], [admin, pw2]]

# 2026-10
vars:
  creds:
    - {username: admin, password: pw1}
    - {username: admin, password: pw2}
prompts:
  - name: login
    send:
      each: vars.creds
      fields:
        - match: 'login:'
          field: username
        - match: 'Password:'
          field: password
```

A prompt with `return: true` and a `send` is now rejected (`return_with_send`). In 2026-08 it was accepted, but it was a shell prompt and its `send` was never used: remove `send`, or remove `return: true` if the prompt should respond.

Everything else is unchanged: `return` without `send`, and a `sendEach` without `fields`. A 2026-08 document fails validation with `unsupported_version` at `autobot`, an old `fields` list with a `fields_entry` error per entry that names the new form (see [`sendEach`](#sendeach)), a `send` list with `send_list`, and a grouped `expect` entry with `grouped_expect` (see [`prompts`](#prompts)).

The single-string `send` and the removal of the list forms and grouped `expect` entries were amended into `2026-10` after its first release, without a new version number. A `2026-10` document written before the amendment that uses a `send` list or a grouped `expect` entry fails validation with `send_list` or `grouped_expect`; migrate it with steps 3 and 4.

`2026-10` was later tightened, also without a new version number, to reject an empty `expect` (`expect: []`, `too_short`) and an empty regex in `expect` or in a `fields` entry's `match` (`string_too_short`); see [`prompts`](#prompts) and [`sendEach`](#sendeach). This only rejects prompts that could never work: `expect: []` never fires, and an empty regex fires at once, before any output. Remove the empty regex, or the prompt if nothing is left.

## CLI

```
autobot [run] <script.yaml> [-a KEY=VALUE ...]
autobot schema
autobot -h | --help
```

Subcommands:
- `run <script>`: load, validate and execute the script. `script` is the path to the YAML script file.
- `schema`: print the JSON schema to stdout (see below). It takes no arguments.

`run` is the default. If the first argument isn't `run`, `schema`, `-h` or `--help`, the CLI treats the command line as `autobot run ...`, so `autobot <script>` is the same as `autobot run <script>`, and options may come before the script (`autobot -a k=v <script>`). A script file named `run` or `schema` must be given with the subcommand (`autobot run schema`) or as a path (`autobot ./schema`).

Options of `run`:

| Flag | Description |
|------|-------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | Pass an argument to the script, accessible as `{{ args.KEY }}`. Repeatable, one `KEY=VALUE` per flag. The value is everything after the first `=`, so it may contain `=`. Values are strings. If a key is given more than once, the last value wins. |
| `-h`, `--help` | Print the `run` usage and exit with status 0. |

`autobot -h` prints the list of subcommands and exits with status 0. `autobot` with no arguments prints the same help to stdout and exits with status 1.

`autobot schema` prints the `2026-10` JSON schema, indented, to stdout, extended with the step types of installed plugins. It reads `schemas/autobot.2026-10.json` from the source tree the CLI runs from (a checkout or an editable install). When that file doesn't exist, as in an installed package, it downloads the schema from `https://raw.githubusercontent.com/mathershifter/autobot/main/schemas/autobot.2026-10.json`, the `main` branch on GitHub, so it needs network access then. It then loads the plugins registered in the `autobot.steps` entry-point group. For each plugin it adds `$defs.<key>Step`, and a `$ref` to it in `$defs.step.oneOf` just before the final `pluginStep` entry. `<key>Step` requires the plugin's key and combines the [common step properties](#common-step-properties), from the static schema's `$defs.stepCommon`, with the plugin model's pydantic JSON schema, which describes the plugin's own fields. Any other key is rejected, unless the plugin's model allows extra fields (the model's `additionalProperties` becomes the definition's `unevaluatedProperties`). Definitions nested in the plugin's schema stay under `$defs.<key>Step.$defs`. When the model refers to itself, its pydantic schema is a `$ref` to its own definition; a copy of that definition takes the `$ref`'s place in `<key>Step`, so the step accepts the common step properties, and the definition stays under `$defs.<key>Step.$defs` for the nested references, which accept only the model's own fields. The plugin's key is also added to the keys `pluginStep` excludes, so a step with that key matches only `<key>Step`: it is checked against the plugin's model and the common step properties, and an invalid one is rejected rather than accepted as an unknown plugin step. A step with a key no installed plugin registers still matches `pluginStep`, which applies `stepCommon` in both the static and the generated schema: its common step properties are checked, and its other keys are not.

A run that completes exits with status 0.

The script file is read as a single YAML document, encoded as UTF-8 (or UTF-16 with a byte order mark). Keys must be unique in every mapping at every level (top level, `attach`, `env`, `vars`, `fn`, prompts, steps, and any nested value). A repeated key is an error rather than overriding the earlier value. Keys are compared as loaded, so `x` and `"x"` are the same key, while `1` (an integer) and `"1"` (a string) are different keys. A key set next to a `<<` merge key overrides the merged value and isn't a duplicate. A mapping can have only one `<<` key; to merge several mappings, use `<<: [*a, *b]`. The CLI reports these load errors on stderr without a traceback:

| Error | First line |
|-------|------------|
| An installed plugin can't be loaded: its entry point fails to import or to create the executor, or the plugin is rejected at registration (an executor without a usable `key`, `model` or `execute`, a reserved key or common-name field, or a key another plugin registered; see [Common Step Properties](#common-step-properties)). Also reported by `autobot schema` | `Plugin error: <message>`, e.g. `Plugin error: entry point 'echo' (distribution pkg-b) failed to load: ModuleNotFoundError: No module named 'foo'` or `Plugin error: plugin pkg_b.EchoExecutor (distribution pkg-b, entry point 'echo'): step key 'echo' is already registered by plugin pkg_a.EchoExecutor (distribution pkg-a, entry point 'echo')` |
| The file can't be read (missing, a directory, permission denied) | `Cannot read script <path>: <reason>` |
| The file isn't valid YAML (syntax error, tab indentation, more than one document, an undefined alias, an unsupported tag such as `!!python/object`, a duplicate key) | `YAML error in <path>, line <L>, column <C>: <problem>`, followed by an indented context line when YAML gives one. For a duplicate key the problem is `found duplicate key '<key>'` at the repeated key, and the context line is `  first defined (line <L>, column <C>)` |
| The file has bytes that aren't valid UTF-8, or disallowed control characters | `YAML error in <path>, position <N>: <reason> (...)` |
| The script fails validation, including an empty file, a document that isn't a mapping, an undefined `call` target or invalid plugin step fields | `Validation errors:`, followed by the details |
| An `--arg` has no `=` | `--arg requires KEY=VALUE format, got: <arg>` |
| The top-level `env` can't be resolved (a template error, a reference cycle, or nesting more than 50 keys deep), a top-level prompt `send` string has a template syntax error, or a top-level prompt's `sendEach` collection can't be resolved (see [`sendEach`](#sendeach)) | `Script error in <path>: <message>`, e.g. `Script error in <path>: template error: ...`, `Script error in <path>: env cycle: A -> B -> A` or `Script error in <path>: prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'` |

Line and column numbers start at 1; `position` is a 0-based offset into the file. For each of these errors the CLI exits with status 1, and nothing runs: `attach.prepare` isn't run and no session is spawned. Only the first error is reported. The installed plugins are loaded first, because validation depends on them, so a broken plugin is reported even when the script itself has an error. Then the file is read and parsed, then validated, then `--arg` values are checked, then `env` and `prompts` (after `--arg`, because `env` may use `{{ args.KEY }}`). Command-line syntax errors caught by the argument parser, such as `--arg` with no value, `run` without a script, an unknown option, or an argument to `schema`, print usage and exit with status 2.

Errors after the script is loaded aren't caught by the CLI: a failed `attach.prepare`, a timeout, a closed connection, an unignored step failure, or a template error at run time. For an error after the spawn wait succeeds, breakouts still run and the session is closed first. When the spawn wait itself fails, no breakout runs; the process is closed first (see [`attach`](#attach)). Then the exception is printed as a Python traceback on stderr, and the CLI exits with status 1. The traceback's last line is the exception type and message, e.g. `EOFError: connection closed while waiting for a shell prompt ('sh')`.
