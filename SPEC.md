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

All fields validated by pydantic against `schemas/autobot.2026-10.json`. The JSON schema is normative: the models reject every document the schema rejects, and accept what it accepts except where a static schema can't decide. One such case is step keys. A static schema can't know which plugins are installed, so its `pluginStep` accepts any object that has no built-in step key. The models accept a step key that isn't built in only if an installed plugin registers it, and otherwise reject the step (`invalid_step`).

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
- `expect` — list of patterns to match against session output. Each entry can be a string (a single-pattern entry) or a list of strings (a grouped entry, e.g. `['login:', 'Password:']`). Entries of both kinds may be mixed. The entry kind decides which response a match sends (see [Response selection](#response-selection)). Required, except in a prompt whose `send` is a `sendEach` with `fields`: there `expect` must be absent, because the patterns come from the `fields` entries (see [`sendEach`](#sendeach)).
- `return` — optional boolean; if `true`, matching this prompt means "we have a shell prompt" and the pending `cmd` is sent. Defaults to `false`. A prompt with no `send` field is also treated as a shell prompt. A prompt with `return: true` must not have `send`, in any form (a flat list, a list of lists, `send: []`, or a `sendEach` with or without `fields`): it would never be sent. That is a validation error (`return_with_send`) at `prompts.N.send`: `a return prompt is a shell prompt and sends nothing; remove send or return`. It is reported together with any other error of the prompt, e.g. `expect_with_fields` or a missing `expect`.
- `send` — optional; responses to send when a pattern matches. Accepts three forms:
  - A flat list of strings: `["response1", "response2"]`
  - A list of lists (grouped attempts): `[["user", "pass"], ["user2", "pass2"]]`
  - A `sendEach` object for data-driven responses (see below)

  The strings of the flat-list and list-of-lists forms are Jinja2 templates. Each is rendered when it is sent, so it sees `vars` registered by earlier steps and `session.*` as last set by an `after` or a shell prompt (the prompt match that triggers the response doesn't set them). A syntax error is reported when the prompts are loaded: before `attach.prepare` runs for the top-level `prompts`, and on entering the block for a block's `prompts`. An undefined variable is reported when the response is sent, prefixed with `prompt '<name>': `, and aborts the step waiting for the prompt. Both are [template errors](#jinja2-templating).

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
- `match` — a regex, or a non-empty list of regexes. The regexes of one list are alternatives for the same prompt: each of them sends the entry's field.
- `field` — the key of the item to send. It is a single mapping key; a `.` in it is part of the key, not a path.

A prompt whose `sendEach` has `fields` has no `expect`: its patterns are the `match` regexes of the entries, in order. So each field is sent only in answer to its own prompt, and the number of values sent per item always matches the prompts that ask for them.

Without `fields`, each item is converted to a string and sent in answer to any of the prompt's `expect` patterns, e.g. a PIN list:
```yaml
- name: pin
  expect: ['PIN:', 'Passcode:']
  send:
    each: vars.pins
```
Here `expect` is required, and each entry must be a single regex: a grouped entry is a validation error (`grouped_expect`, at `prompts.N.expect.M`), since a one-value item can't answer several prompts. Use `fields` for that.

These rules are validation errors, reported like any other before anything runs:

| Rule broken | Location | Type |
|-------------|----------|------|
| `fields: []` | `prompts.N.send.fields` | `too_short` |
| `match: []` | `prompts.N.send.fields.K.match` | `too_short` (`match must be a regex or a non-empty list of regexes`) |
| a `fields` entry without `match` or `field`, or with another key | `prompts.N.send.fields.K.<key>` | `missing` / `extra_forbidden` |
| a `fields` entry that is a string (the 2026-08 form) | `prompts.N.send.fields.K` | `fields_entry` (`since 2026-10 a fields entry pairs a regex with a field: write {match: <regex>, field: <name>} (see "Migrating from 2026-08" in SPEC.md)`) |
| `expect` next to `fields` (even `expect: []`) | `prompts.N.expect` | `expect_with_fields` (`a prompt whose sendEach has fields has no expect: the patterns are the fields' match regexes`) |
| no `expect`, and no `sendEach` with `fields` | `prompts.N.expect` | `missing` |
| a grouped `expect` entry with a `sendEach` without `fields` | `prompts.N.expect.M` | `grouped_expect` (`with sendEach without fields, each expect entry is a single regex; use fields to answer several prompts`) |
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

Every `send` form is a list of *credential sets*, each an ordered list of responses:

| `send` form | Credential sets |
|-------------|-----------------|
| Flat list `["admin", "pw"]` | One set: `["admin", "pw"]` |
| List of lists `[["admin", "pw1"], ["admin", "pw2"]]` | One set per inner list |
| `sendEach` with `fields` | One set per item: the value of each entry's `field`, in entry order |
| `sendEach` without `fields` | One set per item, holding the item converted to a string |

Responses come from the current set, starting with the first. When a pattern of the prompt matches:
- **Grouped entry:** the pattern at position k of its group (the first pattern is position 0) sends item k of the current set.
- **`fields` entry:** a regex of entry k (the first entry is 0) sends item k of the current set, the entry's field. It follows the grouped rules: the entries are one group, and the regexes of one entry share its position, so they count as the same prompt.
- **Single-pattern entry:** sends the first item of the current set that hasn't been sent yet. With a `sendEach` without `fields`, every set holds one item, so a match sends the current item, and any later match of any of the prompt's patterns advances to the next item.

Before sending, the current set advances to the next set when:
- a grouped pattern or `fields` entry picks an item that was already sent from the current set (the group starts over, e.g. `login:` matches again after `Password:`, or `Password:` matches twice), or
- a single-pattern entry matches and every item of the current set has already been sent.

A prompt raises `RuntimeError`:
- `prompt '<name>': responses exhausted` when the set must advance and there is no next set.
- `prompt '<name>': no response available` when the prompt has no credential sets (`send: []`, or `sendEach` over an empty collection), or when the current set of a literal `send` has no item at a grouped pattern's position (the message then names the pattern). A `sendEach` never gives the latter: every `fields` entry has its value in every set.

The selection starts over at the first set, with nothing sent, on every prompt wait (each `get_prompt`).

With `expect: [['login:', 'Password:']]` and `send: [[admin, pw1], [admin, pw2]]`:

| Device prompts (a rejected login restarts the sequence) | Sent |
|---|---|
| `login:`, `Password:`, `login:`, `Password:` | `admin`, `pw1`, `admin`, `pw2` |
| `Password:`, `login:`, `Password:`, `login:` | `pw1`, `admin`, `pw2`, `admin` |
| `Password:`, `Password:` (e.g. `ssh admin@host`) | `pw1`, `pw2` |
| `login:`, `Password:`, `login:`, `Password:`, `login:` | `admin`, `pw1`, `admin`, `pw2`, then `responses exhausted` |

The `sendEach` form of the same login, `fields` entries `login:` → `username` and `Password:` → `password` over `vars.creds: [{username: admin, password: pw1}, {username: admin, password: pw2}]`, sends the same. A password-only login (e.g. `ssh admin@host`) works with the same prompt: the `username` entry just never matches.

With single-pattern entries (`expect: ['login:', 'Password:']`) and the same literal `send`, the responses go out in order across the sets, whichever pattern matched: `admin`, `pw1`, `admin`, `pw2`. That suits devices that always prompt in the same order, and a lone prompt (e.g. a PIN) whose sets each hold one response.

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
| `prepare` | no | Local script to run before spawning the session (e.g. authentication, tunnel setup). Uses the shebang to determine the interpreter. Aborts if the script exits non-zero. Rendered as a Jinja2 template first (see [Jinja2 Templating](#jinja2-templating)). |
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
- The trailing partial line before the prompt match (the prompt prefix) dropped. Output that doesn't end with a newline is therefore not captured.

`errors` patterns are matched with `re.MULTILINE` against the captured output after the shell prompt returns, so they never match the echoed command, and the session is left at the prompt when the error is raised. An error that is printed without a prompt returning results in a timeout rather than an error match.

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
- timeouts: waiting for a prompt, for the `after` pattern, or for the `$?` result
- a closed connection (`EOFError`)
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
| `after` | Expect regex — wait for this pattern before executing. On match, populates `session.before` and `session.match` |
| `when` | Jinja2 conditional — template is rendered, step is skipped if the result is falsy (see below) |
| `delay_before` | Duration to wait before the step |
| `delay_after` | Duration to wait after the step |
| `timeout` | Override default timeout for this step |

`line` and `return` steps do not support `timeout`.

These properties also apply to plugin steps. They are handled by the runner; the plugin's own model receives only its plugin-specific fields. Those fields are validated against the plugin's model when the script is loaded, wherever the step appears (the same places as for `call`). A failure is a validation error at the step's path, e.g. `script.0.ech0` with type `extra_forbidden` for a misspelled field.

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
- prompt `send` strings in the flat-list and list-of-lists forms, each when it is sent (the values `sendEach` takes from `vars` are sent as they are)
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
2. If a prompt with `send` values matches → send the response chosen as described in [Response selection](#response-selection) and continue waiting
3. On 5-second timeout with no match → send a single empty newline to solicit a prompt (once only, and only if no handler has been activated yet)
4. On overall timeout → raise `TimeoutError`

This handles idle consoles that need a return press to display a prompt.

## Migrating from 2026-08

Version `2026-10` changes a `sendEach` with `fields`: each field is now an entry that names the prompt it answers, and the prompt has no `expect`. It also rejects `send` on a `return: true` prompt (see below). To migrate:
1. Change `autobot: 2026-08` to `autobot: 2026-10`.
2. In every prompt whose `send` has `fields` (top-level and block `prompts`), move the patterns from `expect` into the `fields` entries: the pattern that asks for a field becomes that entry's `match`, then remove `expect`.

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

A prompt with `return: true` and a `send` is now rejected (`return_with_send`). In 2026-08 it was accepted, but it was a shell prompt and its `send` was never used: remove `send`, or remove `return: true` if the prompt should respond.

Everything else is unchanged: `expect`, `return` without `send`, the flat-list and list-of-lists `send` forms, and a `sendEach` without `fields` (whose `expect` entries must now be single regexes). A 2026-08 document fails validation with `unsupported_version` at `autobot`, and an old `fields` list with a `fields_entry` error per entry that names the new form (see [`sendEach`](#sendeach)).

## CLI

```
autobot <script.yaml> [--arg KEY=VALUE ...]
```

- `script` — path to the YAML script file
- `--arg` — pass arguments accessible as `{{ args.KEY }}`

The script file is read as a single YAML document, encoded as UTF-8 (or UTF-16 with a byte order mark). Keys must be unique in every mapping at every level (top level, `attach`, `env`, `vars`, `fn`, prompts, steps, and any nested value). A repeated key is an error rather than overriding the earlier value. Keys are compared as loaded, so `x` and `"x"` are the same key, while `1` (an integer) and `"1"` (a string) are different keys. A key set next to a `<<` merge key overrides the merged value and isn't a duplicate. A mapping can have only one `<<` key; to merge several mappings, use `<<: [*a, *b]`. The CLI reports these load errors on stderr without a traceback:

| Error | First line |
|-------|------------|
| The file can't be read (missing, a directory, permission denied) | `Cannot read script <path>: <reason>` |
| The file isn't valid YAML (syntax error, tab indentation, more than one document, an undefined alias, an unsupported tag such as `!!python/object`, a duplicate key) | `YAML error in <path>, line <L>, column <C>: <problem>`, followed by an indented context line when YAML gives one. For a duplicate key the problem is `found duplicate key '<key>'` at the repeated key, and the context line is `  first defined (line <L>, column <C>)` |
| The file has bytes that aren't valid UTF-8, or disallowed control characters | `YAML error in <path>, position <N>: <reason> (...)` |
| The script fails validation, including an empty file, a document that isn't a mapping, an undefined `call` target or invalid plugin step fields | `Validation errors:`, followed by the details |
| An `--arg` has no `=` | `--arg requires KEY=VALUE format, got: <arg>` |
| The top-level `env` can't be resolved (a template error, a reference cycle, or nesting more than 50 keys deep), a top-level prompt `send` string has a template syntax error, or a top-level prompt's `sendEach` collection can't be resolved (see [`sendEach`](#sendeach)) | `Script error in <path>: <message>`, e.g. `Script error in <path>: template error: ...`, `Script error in <path>: env cycle: A -> B -> A` or `Script error in <path>: prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'` |

Line and column numbers start at 1; `position` is a 0-based offset into the file. For each of these errors the CLI exits with status 1, and nothing runs: `attach.prepare` isn't run and no session is spawned. Only the first error is reported. The file is read and parsed, then validated, then `--arg` values are checked, then `env` and `prompts` (after `--arg`, because `env` may use `{{ args.KEY }}`). Command-line syntax errors caught by the argument parser, such as `--arg` with no value, print usage and exit with status 2.
