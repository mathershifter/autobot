# Autobot — Console Robot Script Engine

Autobot executes YAML-defined scripts against remote consoles via pexpect. It handles login negotiation, multi-hop session management, and cleanup automatically.

## Architecture

```
YAML script → pydantic validation → Runner → Session (pexpect) → remote console
```

- **YAML script** defines environment, credentials, prompt patterns, functions, attach config, and a step-based script.
- **Pydantic models** (`Config` and subtypes) validate and parse the YAML against `schemas/autobot.2026-10.json`. They also check what a JSON schema can't express: every `call` target is defined in `fn`, every plugin step's fields are valid for its plugin model, and every regex that can be checked before the run compiles. All of this happens when the script is loaded, before `attach.prepare` runs or anything is spawned.
- **Runner** renders Jinja2 templates, dispatches steps, and manages block breakouts.
- **Session** wraps pexpect, handles prompt detection and credential cycling.

## YAML Script Structure

All fields validated by pydantic against `schemas/autobot.2026-10.json`. The JSON schema is normative: the models reject every document the schema rejects, and accept what it accepts except where a static schema can't decide. One such case is step keys. A static schema can't know which plugins are installed, so its `pluginStep` accepts any object that has no built-in step key and whose [common step properties](#common-step-properties) are valid (it applies `$defs.stepCommon`); the step's other keys aren't checked. The models accept a step key that isn't built in only if an installed plugin registers it, and otherwise reject the step (`invalid_step`). They also reject a step with more than one plugin key, which the static schema accepts as a `pluginStep` (see [Common Step Properties](#common-step-properties)).

Another such case is regular expressions. A JSON schema can't check Python regex syntax, so only the models compile them. Every regex that is used as written is compiled when the script is loaded: the top-level `errors`, and a prompt's `expect` and the `match` of its `fields` entries, in top-level and block `prompts`. One that doesn't compile is a validation error of type `invalid_regex` at the value (e.g. `errors.1`, `prompts.0.expect`, `prompts.0.expect.1`, `prompts.0.send.fields.0.match.1`), with the message `invalid regex '<regex>': <reason>`, e.g. `invalid regex '(': missing ), unterminated subpattern at position 0`. `assert` and `after` are templates. One that contains no Jinja2 syntax (`{{`, `{%` or `{#`) is checked the same way when the script is loaded, at `script.N.cmd.assert` (the first bad pattern of a list) or the step's `after`. One that does is checked when it is used, once rendered: a result that isn't a valid regex is a `ValueError`, `assert: invalid regex '<rendered>': <reason>` or `after: invalid regex '<rendered>': <reason>`, chained from the `re.error`. So an invalid regex never surfaces as a raw `re.error`, and one that can be known before the run stops the script before `attach.prepare` runs or anything is spawned.

A third case is numbers that JSON doesn't have. The schema is applied to the document as YAML loads it, and there a number can be `.inf`, `-.inf` or `.nan`, or an integer beyond any double. The schema's `minimum` and `maximum` reject the infinities and every number above the largest double, as the models do. But no bound compares with `.nan`, and a pattern can't compute the value a duration string stands for, so only the models reject `.nan` and a duration string whose value overflows to infinity (see [Duration Format](#duration-format)).

A fourth case is the command line of `attach.spawn`: the schema's pattern rejects an empty or blank `spawn`, but only the models can split the command line and see that quotes or a backslash leave it without a command (`empty_command`, see [`attach`](#attach)).

A fifth case is mapping keys. Every key of a script is a string. YAML has other keys as well: `1:` is an integer, `1.5:` a number, `true:` a boolean, `~:` a null, `2026-01-01:` a date, and a `!!binary` key is binary data. Like a value, a key is never converted from another type, so `vars: {1: x}` is a validation error and not the key `"1"`; quote the key (`"1": x`) to get that. This holds for every mapping the script's structure defines:
- The mappings whose keys the script chooses: `env`, `vars`, `fn` and `attach.env`. The models report `string_type` at the key, e.g. `vars.1.[key]`.
- The mappings with fixed keys: the top level, `attach`, a prompt, a `sendEach`, a `fields` entry, a function, a block and every step, plugin steps included. A key that isn't a string can't be one of their keys, and the models report `invalid_key`.

The mappings inside a `vars` value are data, not structure, and their keys are whatever YAML makes of them: `vars: {ports: {1: up}}` is valid, and `{{ vars.ports[1] }}` reads it. JSON has only string keys, so a JSON schema says nothing about a key's type by itself. The schema states the rule with `propertyNames: {type: string}`, on the four mappings above and on `$defs.stepCommon` (for plugin steps; the other mappings are closed, and a key that isn't a string is an additional property). That rejects the same documents as the models when the schema is applied to the document as YAML loads it, with a validator that hands it each key as it is (Python's `jsonschema` does). A validator that reads the document as JSON first, or an editor that takes `1:` for the key `"1"`, sees only strings and accepts it; there only the models reject it.

A value has the type the schema names; it is never converted from another type. A list is a YAML sequence: a `!!set`, or any other value that isn't a list, where a list goes (`script`, `errors`, a list of `cmd` lines, `expect` and so on) is a validation error (`list_type`). A string is a YAML string: a `!!binary` value isn't one, so `cmd: !!binary aGk=` or a `!!binary` `spawn` is a validation error (`string_type`) in the models as in the schema, wherever a string goes. A boolean is `true` or `false`, not `1` or `"yes"`. Numbers follow JSON, where a number with a zero fraction is that integer: `return: 1.0` is `return: 1` for both.

An optional field is either omitted or given a value of its type. An explicit `null`, including a key with an empty YAML value (`after:`, `timeout: ~`), is invalid for every optional field, including the common step properties of plugin steps: omit the key instead to get the default. The error type is `null_value`, located at the key. A plugin's own fields follow the plugin's model.

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `autobot` | yes | Schema version: exactly `2026-10`. Surrounding whitespace or a trailing newline is rejected. Any other value is a validation error (`unsupported_version`) at `autobot`: `unsupported autobot version '<value>'; expected 2026-10`, or for the earlier version `2026-08`, `autobot 2026-08 is no longer supported; use 2026-10`. That includes a value that isn't a string, e.g. what YAML makes of an unquoted `2026` (a number), `2026.10` (the number 2026.1) or `2026-10-04` (a date): the message shows the value as text, `unsupported autobot version '2026'; expected 2026-10`. Only a missing key (`missing`) and an explicit null (`string_type`) are reported as what they are. |
| `env` | no | Defaults for environment variables: string values under string keys. In a template, `env` is the whole environment Autobot was started with, so `{{ env.HOME }}` reads any variable, whether it is a key of this section or not (see [The environment in templates](#the-environment-in-templates)). A key of this section is a default: it gives the variable a value where the environment doesn't set it. A variable that the environment sets keeps its value, even an empty one, and its default isn't used. The defaults are for templates only: they aren't added to the environment of the spawned process (see [`attach`](#attach)). Supports nesting: a default may reference other variables (e.g. `{{ env.OTHER_KEY }}`, `{{ env['OTHER_KEY'] }}`), keys of this section in any order and variables of the environment alike. Each default is rendered once, after the values it references; the text it renders to isn't rendered again. Any read of a variable's value counts as a reference, including `env.get('KEY')` and `env.items()`. A variable of the environment is used exactly as it is set: its value isn't a template and is never rendered, so `{{` in it is plain text, and a default that references it gets that text. The default it overrides isn't used, so the default's references aren't followed. A reference cycle, a default that references itself directly or through other keys, is an error naming the cycle (`env cycle: A -> B -> A`), and so is a chain more than 50 keys deep. A reference to a variable that the environment doesn't set and that isn't a key of `env` is an undefined variable (`template error: env has no key 'KEY'`), so `default` applies to it. Accessible as `{{ env.KEY }}` |
| `vars` | no | Arbitrary objects under string keys, accessible as `{{ vars.KEY }}` |
| `prompts` | no | Named prompt/response definitions for interactive sessions |
| `errors` | no | Regex patterns for CLI error detection (e.g. `% .*`). When defined, replaces `$?` exit code checking. Each pattern is a valid, non-empty regex: an empty one (`''`) would match any output and fail every command, so it is a validation error (`string_too_short` at `errors.N`: `an errors pattern must not be empty: an empty regex matches any output, so every command would fail`), and one that doesn't compile is `invalid_regex`. `errors: []` is the same as no `errors`. |
| `fn` | no | Named functions (reusable step sequences) |
| `attach` | yes | Session spawn and lifecycle config |
| `script` | yes | Ordered list of steps to execute |

### `prompts`

Each prompt has:
- `name` — identifier
- `expect` — a regex, or a non-empty list of regexes, to match against session output. The regexes are alternatives: any of them triggers the prompt. A single regex is the same as a list holding it. Every entry of the list is a single regex: a list inside `expect` is a validation error (`grouped_expect`, see below). `expect: []` would never fire, and an empty regex (`''`) matches at once, before any output, so both are validation errors (see below). Required, except in a prompt whose `send` is a `sendEach` with `fields`: there `expect` must be absent, because the patterns come from the `fields` entries (see [`sendEach`](#sendeach)).
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

These are validation errors at the prompt, reported like any other before anything runs:

| Rule broken | Location | Type and message |
|-------------|----------|------------------|
| `send` is a list (of strings, of lists, or empty) | `prompts.N.send` | `send_list`: `send is a single string: for a simple prompt write send: '<response>'; to answer a sequence of prompts such as a login, use sendEach with fields and keep the values in vars` |
| `send` is a boolean or a number (e.g. unquoted `send: yes`) | `prompts.N.send` | `send_type`: `send must be a string; quote it, e.g. send: 'yes' or send: '1234' (unquoted, YAML reads yes, no, on, off, true, false and numbers as booleans or numbers)` |
| an `expect` entry is a list | `prompts.N.expect.M` | `grouped_expect`: `each expect entry is a single regex, and the regexes are alternatives; to answer a sequence of prompts such as a login, use sendEach with fields` |
| `expect: []` | `prompts.N.expect` | `too_short`: `expect must be a regex or a non-empty list of regexes` |
| `expect: ''`, or an `expect` entry `''` | `prompts.N.expect`, or `prompts.N.expect.M` for an entry | `string_too_short`: `a regex must not be empty: an empty regex matches at once, before any output` |
| `expect`, or an `expect` entry, isn't a valid regex | `prompts.N.expect`, or `prompts.N.expect.M` for an entry | `invalid_regex`: `invalid regex '<regex>': <reason>` (models only, see [YAML Script Structure](#yaml-script-structure)) |

In a prompt whose `sendEach` has `fields`, an empty `expect` (`[]`, `''` or `['']`) is reported only as `expect_with_fields` (see [`sendEach`](#sendeach)): there `expect` must be absent. The empty-`expect` errors are reported together with the prompt's other errors, such as `return_with_send` or `grouped_expect`.

An invalid `send` (`send_list`, `send_type`) is reported on its own: the prompt's other rules, such as `return_with_send`, `grouped_expect`, a missing or empty `expect`, are checked only once `send` is valid. So a login with a grouped `expect` and a `send` list reports `send_list` first. The JSON schema rejects the same documents. A block's prompts are validated the same way, at `script.N.block.block.prompts.M...`.

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
| `match`, or a `match` entry, isn't a valid regex | `prompts.N.send.fields.K.match`, or `prompts.N.send.fields.K.match.M` for an entry | `invalid_regex` (`invalid regex '<regex>': <reason>`) |
| a `fields` entry without `match` or `field`, or with another key | `prompts.N.send.fields.K.<key>` | `missing` / `extra_forbidden` |
| a `fields` entry that is a string | `prompts.N.send.fields.K` | `fields_entry` (`a fields entry pairs a regex with a field: write {match: <regex>, field: <name>}`) |
| `expect` next to `fields` (even `expect: []` or `expect: ''`) | `prompts.N.expect` | `expect_with_fields` (`a prompt whose sendEach has fields has no expect: the patterns are the fields' match regexes`) |
| no `expect`, and no `sendEach` with `fields` | `prompts.N.expect` | `missing` |
| `return: true` with a `sendEach` (with or without `fields`), or any other `send` | `prompts.N.send` | `return_with_send` (`a return prompt is a shell prompt and sends nothing; remove send or return`) |

The JSON schema states all of them except `invalid_regex`, which only the models can check, so apart from that the models and the schema reject the same documents. A block's prompts are validated the same way, at `script.N.block.block.prompts.M...` (the step's type tag comes before its key).

In the example, `vars.creds` is resolved, and each item becomes one credential set: the value of each entry's field, in entry order. The collection is resolved when the prompts are loaded (at script start for the top-level `prompts`, on entering the block for a block's `prompts`, so a block sees `vars` registered by earlier steps), and its values are sent as they are, never rendered as templates.

`each` is a path under `vars`: `vars` followed by one or more `.`-separated keys (e.g. `vars.creds`, `vars.site.creds`). Each key names a key of a mapping; list indexes, attributes, `env`, `args` and `session` can't be used. Any other form is a validation error.

The collection must be a list. Without `fields`, each item must be a string or number. With `fields`, each item must be a mapping with the `field` of every entry, and each field's value must be a string or number. An empty list is allowed; the prompt then has no credential sets (see [Response selection](#response-selection)).

A string is sent as it is, and a number as YAML's loader reads it (`1234`, `2.5`; `007` is the number 7 and is sent as `7`). A boolean is never sent: unquoted `true`, `false`, `yes`, `no`, `on`, `off` and their capitalized forms are booleans in YAML, there is no one text for them, and an item or field value that is one is an error (see below). Quote a value to send it as written: `'true'`, `'yes'`, `'007'`.

A collection that doesn't meet these rules is an error when the prompts are loaded: before `attach.prepare` runs for the top-level `prompts` (reported by the CLI as a `Script error`), and on entering the block, before its `enter` steps, for a block's `prompts` (the run stops; the block's `breakout` doesn't run, and `attach.breakout` does). The message is `prompt '<name>': sendEach '<each>': <problem>`, where `<problem>` is one of:

| Problem | Example |
|---------|---------|
| A key is missing | `no key 'nope' in 'vars.site'` |
| A value on the path isn't a mapping | `'vars.site' is a string, not a mapping` |
| The collection isn't a list | `'vars.creds' is a mapping, not a list` |
| An item isn't a mapping (with `fields`) | `item 2 is a string, not a mapping` |
| An item lacks a field | `item 2 has no field 'password'` |
| A field's value isn't a string or number | `item 2 field 'password' is null, not a string or number` |
| An item isn't a string or number (without `fields`) | `item 0 is a mapping; without fields each item must be a string or number` |
| An item is a boolean (without `fields`) | `item 1 is a boolean (true), which is never sent as text; quote the value to send it as written, e.g. 'true' or 'yes'` |
| A field's value is a boolean | `item 2 field 'password' is a boolean (false), which is never sent as text; quote the value to send it as written, e.g. 'true' or 'yes'` |

Items are counted from 0. Types are named as in YAML: `a mapping`, `a list`, `a string`, `a number`, `a boolean`, `null`, and for the other values YAML can produce, `a timestamp`, `binary data` and `a set`. Those are never sent: an unquoted date such as `2026-10-04` is a timestamp, not a string, so `item 1 is a timestamp; without fields each item must be a string or number`. Quote it to send it as written.

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

A `sendEach` prompt raises `RuntimeError` (a `RunError`, see [CLI](#cli)):
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
| `prepare` | no | Local script to run before spawning the session (e.g. authentication, tunnel setup). Rendered as a Jinja2 template first (see [Jinja2 Templating](#jinja2-templating)). Leading spaces, tabs, line breaks, a byte order mark (U+FEFF) and the zero-width characters U+200B and U+2060 are removed from the rendered script; what remains is written to a temp file. The script is written as UTF-8, whatever the locale's encoding. If it starts with `#!`, the script is executed directly and the shebang picks the interpreter (a `\r` ending the shebang line is dropped). Otherwise it runs under `/bin/sh`, and the progress line says so: `>> prepare: running local script (no shebang, using /bin/sh)`. Aborts if the script exits non-zero (`prepare script failed with exit code <N>`). If the script can't be started (e.g. the interpreter is missing or not executable), aborts with `prepare script could not run ('<first line>'): [Errno <n>] <reason>`, chained from the `OSError`. The temp file is removed in every case, including when the script can't be written to it (e.g. a `UnicodeEncodeError` for a character UTF-8 can't encode, such as the lone surrogate a non-UTF-8 byte of an `--arg` becomes), and nothing is spawned after a failure. |
| `spawn` | yes | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`). Rendered as a Jinja2 template. It must name a command: an empty or blank string (`''`, `'  '`) is a validation error. Blank means nothing but whitespace, and whitespace is the same fixed set of characters in the schema and the models: U+0009 to U+000D, U+001C to U+001F, the space, U+0085, U+00A0, U+1680, U+2000 to U+200A, U+2028, U+2029, U+202F, U+205F and U+3000 (so a byte order mark, U+FEFF, isn't whitespace). The error is `empty_command` at `attach.spawn`: `spawn must be a command, not an empty or blank string`. The command line is split into words as pexpect does it (whitespace separates words; `'...'`, `"..."` and `\` quote), and its first word, the command, must not be empty either: a `spawn` of nothing but quotes or a backslash (`"''"`, `'""'`, `'\'`) has no words at all, and `'' ls` has an empty first one. That is `empty_command` too, with the message ``spawn must name a command: the first word of <value> is empty (quotes or a backslash with nothing in them)`` (the value as a Python string literal), and only the models report it: a schema pattern can't split a command line (see [YAML Script Structure](#yaml-script-structure)). Whitespace before the command is not part of the command line. It is removed from the `spawn`, as written or once rendered, before the command line is checked, split and spawned, so `spawn: ' ssh host'` and `spawn: "{{ args.wrapper | default('') }} ssh host"` without a `wrapper` both run `ssh host` (pexpect by itself takes a leading space for an empty first word and finds no command). The progress line and the spawn-wait errors show the command line without that whitespace. What follows the whitespace must still name a command: `" ''"` and `" '' ls"` are `empty_command` like `"''"` and `"'' ls"`. A `spawn` with Jinja2 syntax (`{{`, `{%` or `{#`) is checked for this once it is rendered. A template that renders to an empty or blank string, or to a command line whose first word is empty, is a `ValueError` when the run starts, `attach.spawn rendered to an empty command: '<template>'`, before `attach.prepare` runs. |
| `timeout` | no | Timeout for the initial spawn (duration) |
| `env` | no | Environment variables for the spawned process. Replaces the full process environment (not merged with the parent). If omitted, defaults to `TERM=dumb` and `NO_COLOR=1`. An empty map (`env: {}`) is not omitted: the process gets an empty environment. An empty value (`env:`) is invalid, like any explicit `null`. The `spawn` command is looked up in the `PATH` of `env`, or in the system default path (`/bin:/usr/bin` on Linux) when `env` has no `PATH`, as with the default; give a full path or set `PATH` for commands elsewhere. |
| `script` | no | Steps to run immediately after spawn (before main script) |
| `breakout` | no | Steps to run in `finally` after the main script (cleanup/disconnect) |

The attach lifecycle:
1. `attach.prepare` runs locally (if defined) — aborts on failure
2. `pexpect.spawn(attach.spawn)` — waits up to `attach.timeout` (default 300s) for initial output. The output is left unconsumed, so a login or shell prompt that arrives with the banner is handled by the first prompt wait.
3. `attach.script` steps execute (e.g. jump-host commands)
4. Main `script` steps execute
5. `attach.breakout` steps execute (best-effort, errors logged to stderr)
6. Session closed

Steps 5 and 6 run after step 3 or 4 fails, and step 6 runs even if the breakout fails. A breakout error never replaces an error raised by `attach.script` or `script`.

Closing the session (step 6, and the close after a failed spawn wait) closes the process's pty and terminates a process that is still running. It can fail: pexpect raises `ExceptionPexpect` (`Could not terminate the child.`) for a process that survives `SIGHUP`, `SIGINT` and `SIGKILL`. Then:
- The session forgets the process either way. It isn't closed a second time, and nothing more can be sent to it.
- If an error is already propagating (from the spawn wait, `attach.script` or `script`), the close failure never replaces it. It is logged to stderr, `>> close error (ExceptionPexpect): Could not terminate the child.`, and the original error propagates, as with a breakout error.
- If nothing else failed, the close failure is the run's error: it propagates, and the CLI reports a failed run, `Run failed in <path>: Could not terminate the child.`, and exits with status 3 (see [CLI](#cli)). The script's steps all ran, but a process that couldn't be terminated may still hold the line, so the run doesn't report success.

A `KeyboardInterrupt` during the close is not held back in either case.

If the spawn wait (step 2) fails, the run stops there. Neither `attach.script` nor `script` runs, and **`attach.breakout` doesn't run**: the breakout undoes what the steps did on the remote (log out, leave a console server session), and no step has run or sent anything. The spawned process, if any, is closed: its pty is closed, and a process that is still running is terminated (`SIGHUP` and `SIGINT`, then `SIGKILL` if it ignores them). Closing the process is what frees the line; a silent `ssh` or `telnet` that is killed drops its connection. Then the error propagates (the CLI reports a failed run and exits with status 3, or 130 for an interrupt; see [CLI](#cli)). The spawn wait fails when:
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

`cmd` accepts a string or list of strings. A string is rendered as a Jinja2 template as a whole, then split into lines on newlines, and blank lines of the result are skipped. A newline is a line feed (`\n`), a carriage return and line feed (`\r\n`), or a carriage return alone (`\r`, which a terminal takes as Return); the newline itself isn't part of either line. No other character ends a line: a form feed, a vertical tab, U+001C to U+001E, U+0085, U+2028 or U+2029 inside a command is sent as part of its line (what the remote terminal does with a control character it receives is up to it). A blank line is one that is empty or has nothing but whitespace (the set of characters listed for [`attach.spawn`](#attach)), so a newline at the end of the string adds no line. Every other line is sent as written, with its leading and trailing whitespace. So a Jinja2 block (e.g. `{% for %}`...`{% endfor %}`) may span lines, and a template that expands to several lines sends each of them. A string that renders to nothing but blank lines sends one empty line (like `cmd: ""`). Each item of a list is rendered on its own and split the same way, and the lines of all items are sent in order; a Jinja2 block can't span items. The whole `cmd` is rendered once, after the step's first prompt wait and before its first line is sent, so a template error sends nothing and no line sees the output of an earlier line of the same step. Each line waits for a prompt before sending.

A `cmd` with [`after`](#common-step-properties) waits for the pattern and then for a prompt, in that order: the `after` match is not a prompt, and the command is never sent on the match alone. So a pattern that matches while an earlier command or `line` is still running, before its prompt, doesn't make that prompt the command's own: the step's first prompt wait consumes it, the command is sent at the prompt, and its captured output is only its own. This first prompt wait differs from an ordinary one (see [Prompt Handling](#prompt-handling-get_prompt)) in two ways:
- It never sends the solicit newline, however long the prompt takes. The `after` match is output, so the console isn't idle, and a Return pressed here would go to whatever is still running: a command, which would answer it with a second prompt, or a question such as `Proceed? [y/N]`, which would take it for an answer. The wait lasts until a shell prompt arrives or the step's `timeout` expires. Then the step fails with `TimeoutError`, `timed out after <timeout>s waiting for a shell prompt ('<name>', ...) after the after pattern matched (a cmd is sent at a shell prompt; use line to send without one)`.
- It doesn't change `session.before` and `session.match`, so the `cmd` is rendered with what `after` matched, as `when` was (the wait after the command sets them as usual).

If the session is at a shell prompt when `after` matches, the wait returns at once and the command is sent right after the match. That includes a match that itself ends at the prompt: when the text the `after` wait read ends with a shell prompt of the current `prompts`, with nothing read after it, the session is at that prompt (e.g. `after: 'done\r\nadmin@host:~\$ $'`, or the prompt's own regex). A pattern that takes only part of the prompt, or something after it, leaves no prompt to wait for, and the step times out as above. To send on the match where there is no shell prompt, e.g. to answer a question or a boot loader's countdown, use `line`, which never waits for one.

`cmd: []` has no lines, and it is the only `cmd` that has none: an empty or blank string, also as an item of a list (`cmd: ""`, `cmd: [""]`, a template that renders to nothing), is one empty line. A step with no lines sends nothing at all:
- It makes no prompt wait, not even the step's first one: a prompt wait may send a solicit newline or answer a prompt with `send` (see [Prompt Handling](#prompt-handling-get_prompt)). `session.before` and `session.match` keep their values.
- There is no `$?` check, since there is no last line whose return code it would read, and no `errors` check, since no line has output.
- The captured output is empty. `assert`, if defined, is rendered and checked against that empty output as usual, so the step fails with `assertion failed` unless a pattern matches the empty string (e.g. `^$`); `ignore_error` covers that failure. Without `assert` the step can't fail.
- `register` stores the empty string.
- The [common step properties](#common-step-properties) apply as for any step: `when`, `delay_before` and `delay_after`, and `after`, which is waited for before the step and is then the only thing the step waits for.

After each command line, the step waits for a shell prompt. If top-level `errors` patterns are defined, that line's captured output is checked against them; on a match the step raises and no further lines are sent.

After the last command line, the step:
1. If `assert` is defined, checks the captured output of all lines for a matching pattern — raises if none match
2. If `assert` is not defined and no `errors` are defined, checks the return code of the last line via `echo $?` — raises on non-zero

When `assert` is defined, it replaces the return code check — the assertion pattern is the success criteria. If top-level `errors` patterns are defined, they replace the `$?` check.

The `$?` check sends `echo __AUTOBOT_RC=$?` and reads the digits that follow the marker in the output. It waits for the character after the last digit (the line break, usually) before it takes the number, so an exit code that arrives in pieces (`1`, then `27`) is read whole, as 127. The echo of the check itself has no digit after the marker and is never taken for the result.

`assert` is a regex or a list of regexes, each rendered as a template after the last line's prompt returns and searched for in the captured output (`re.search`, no flags). An empty pattern would match any output, so the step would check nothing:
- `assert: ''`, or an empty entry in the list, is a validation error (`string_too_short` at the step's `assert`: `an assert pattern must not be empty: an empty regex matches any output, so the assert would check nothing`).
- A pattern that renders to an empty string aborts the step with a `ValueError`, `assert: a pattern rendered to an empty regex, which matches any output`. Like an invalid regex, it isn't a command failure, so `ignore_error` doesn't cover it.

`assert: []` is accepted and is the same as no `assert`, as `cmd: []`, `line: []` and `control: []` are accepted and send nothing (for `cmd: []`, see above). A pattern that isn't a valid regex is an error when the script is loaded or, for a template, when it is rendered (see [YAML Script Structure](#yaml-script-structure)).

#### Captured output

The captured output of a command line is the text the session prints between sending the line and the next shell prompt, with:
- The terminal echo of the sent command removed. The echo is matched ignoring whitespace and `\r`, across wrapped lines, and in readline's horizontal-scroll form (`\r<` + visible tail) used for long lines. If the echo doesn't match (e.g. echo disabled), the output is left unchanged.
- Line endings normalized to `\n`.
- ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)).
- Stray carriage returns, NULs and BELs removed where they come first in the unread output, i.e. at the start of a line or right after an escape sequence, and something other than a line break follows them (see [Prompt Handling](#prompt-handling-get_prompt)): `\rtwo\rthree` is captured as `two\rthree`. Everywhere else NULs and BELs are kept, and so are carriage returns, with one exception: carriage returns right before a line break are dropped with it, so `out\r\r` and a line break is captured as `out\n`, while `\x00` and a line break is `\x00\n`.
- The trailing partial line before the prompt match (the prompt prefix) dropped. Output that doesn't end with a newline is therefore not captured.

`errors` patterns are matched with `re.MULTILINE` against the captured output after the shell prompt returns, so they never match the echoed command, and the session is left at the prompt when the error is raised. An error that is printed without a prompt returning results in a timeout rather than an error match.

#### ANSI escape sequences

The session removes ANSI escape sequences that match `types.ANSI_ESCAPE_RE`: CSI sequences (`ESC [`, parameter and intermediate bytes, a final byte; e.g. colors, `ESC [?2004h`, cursor movement) and two-byte `ESC` sequences whose second byte is `@` through `Z` or `\` through `_`. Other sequences are left in the text, e.g. `ESC ( B`, `ESC =`, `ESC 7`, the 8-bit CSI byte `0x9B`, and the body of an OSC sequence such as a terminal title (only its `ESC ]` is removed). `attach.env` defaults to `TERM=dumb` and `NO_COLOR=1`, so many programs print no color to begin with.

Stripped text, which never contains a removed sequence:
- The captured output of a command (see above), and so the text `assert` and `errors` patterns are matched against, the value `register` stores, and `session.before` after a shell prompt.
- The session output echoed to the operator. Everything read from the session is written to stdout with the sequences removed (`session.CleanWriter`), while autobot's own `>> ...` messages and errors go to stderr (see [Output](#output)). A sequence that arrives in two reads is removed like any other: when a read ends in the start of a sequence (an `ESC`, or `ESC [` with parameter and intermediate bytes but no final byte yet), the echo writes the text before it and holds the start back until the next read completes it. What is still held when the session is closed is written out then, as it is. At most 64 characters are held; a longer run after an `ESC` is no real sequence and is written out at once.

Raw text, with escape sequences as received:
- `after` patterns are matched against the raw output stream, and the `session.before` and `session.match` set by an `after` match are raw: escape sequences, `\r\n` line endings and the command echo are all kept. A pattern such as `AABBCC` doesn't match output printed as `AA ESC[1m BB ESC[0m CC`; match around the sequence instead (e.g. `AA.*CC`).
- Prompt `expect` regexes (and the `match` regexes of `sendEach` `fields` entries) are matched against the raw stream too, but while it waits for a prompt the engine also consumes each line break and each escape sequence as it arrives, and discards stray `\r`, NUL and BEL characters at the start of the unread output (see [Prompt Handling](#prompt-handling-get_prompt)). A prompt regex should therefore match text that comes after the prompt's last escape sequence and before any that follows it. For a prompt printed as `ESC[32m PS1> ESC[0m`, `PS1> ` matches, but `PS1> $` doesn't when the `ESC[0m` arrives together with the prompt, as it usually does: it is still in the stream after `PS1> ` when the regex is tried. A regex that spans an escape sequence or a line break may or may not match, depending on how the output arrives. `session.match` after a shell prompt is the text the prompt regex matched.
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
- timeouts: waiting for a prompt, for the `after` pattern, or for the `$?` result. The `TimeoutError` messages are `timed out after <timeout>s waiting for a shell prompt ('<name>', ...)` (the names of the current shell prompts, or `none defined`), `... waiting for the after pattern '<pattern>'` (the rendered pattern) and `... waiting for the exit code of the command (echo $?)`; the prompt wait that follows an `after` match adds ` after the after pattern matched (a cmd is sent at a shell prompt; use line to send without one)`
- a closed connection (`EOFError`). The message starts with `connection closed` and names what was being waited for, as the timeouts do: `connection closed while waiting for a shell prompt ('<name>', ...)`, `... while waiting for the after pattern '<pattern>'` and `... while waiting for the exit code of the command (echo $?)`
- template errors (`template error: ...`, see [Jinja2 Templating](#jinja2-templating)), including in a prompt `send` response, and an `assert` or `after` that renders to an invalid regular expression or to an empty one (`assert: invalid regex ...`, `after: invalid regex ...`, `assert: a pattern rendered to an empty regex ...`, `after: the pattern rendered to an empty regex ...`; an invalid regex that isn't a template never gets this far, it fails validation)
- prompt-response failures (`responses exhausted`, `no response available`)

#### Capturing output with `register`

Set `register` to store the command's captured output into `vars.<name>`, making it available to subsequent steps via Jinja2 templates as `{{ vars.<name> }}`. The stored value is the output text with leading and trailing whitespace stripped. A step that sent nothing (`cmd: []`) stores the empty string. The name is used as written (it isn't a template) and must not be empty: `register: ''` is a validation error (`string_too_short` at the step's `register`), rather than a step that silently stores nothing.

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

Jinja2 templating, `assert`, `ignore_error`, and `timeout` all work normally with embedded scripts. Like any `cmd`, the script is rendered once, after the step's first prompt wait and before anything is sent, so `session.before` and `session.match` in it are what that wait set (or what `after` matched, in a step with `after`), and a template error uploads nothing. Embedded scripts must be a single string, not a list.

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
4. `breakout` steps execute in `finally` (best-effort, errors logged to stderr)
5. If `prompts` was defined, restore the previous session handlers

Steps 4 and 5 run after step 2 or 3 fails, so the breakout runs and the prompts are restored even when `enter` fails, and step 5 runs even if the breakout fails. A breakout error never replaces an error raised by `enter` or `script`.

#### Prompt state across a swap

The swap (step 1) and the restore (step 5) neither read from the session nor send anything. They change only whether the session counts as being at a shell prompt, that is, whether the next prompt wait (e.g. the first wait of a `cmd`) returns at once (see [Prompt Handling](#prompt-handling-get_prompt)):

- If the session is at a shell prompt, the text of that prompt is checked against the new prompts the way a prompt wait would read it. The text is the prompt's line as the last prompt wait matched it: from the end of the last line break or escape sequence before the match, up to the end of the match, escape sequences in the match included. As in a prompt wait, the match that starts earliest in that text wins, among `\r\n`, escape sequences, the new prompts' regexes and stray `\r`, NUL and BEL characters at its start, with ties broken the same way. A winning line break, escape sequence or run of stray characters is skipped and the rest of the text is checked again. If the winner belongs to a shell prompt, the session stays at the prompt: the next `cmd` sends at once, without the 5-second idle wait or a solicit newline. If it belongs to a prompt with `send`, or nothing matches, the session no longer counts as at a prompt. So does a new prompt regex that isn't valid; the next prompt wait reports the error. (A script's prompts can't have one, since they are checked when the script is loaded; this concerns handlers built in code, e.g. by a plugin.)
- If the session isn't at a shell prompt (e.g. after a `line`, `return` or `control` step, or a step that timed out), it stays that way. A swap or restore never puts the session at a prompt.

So a block whose prompts recognize the prompt on screen, e.g. one that only adds a handler for a confirmation question, or nests a block with the same shell prompt, costs no extra wait or newline on entry or exit. Captured output is unaffected: the check consumes no output, and `session.before` and `session.match` keep the values of the last prompt wait.

When the session doesn't count as at a prompt, the next prompt wait reads new output with the new prompts, and sends its one solicit newline if nothing matches by its first poll timeout (5 seconds, or the wait's timeout if that is shorter), unless the wait follows a command (see [Prompt Handling](#prompt-handling-get_prompt)). A prompt that the active prompts don't recognize is never used to send a command, and an idle shell answers the solicit newline with the same prompt again. So:
- A `cmd` as the first step of a block whose prompts don't recognize the current prompt times out. Enter the sub-CLI with `line` (it sends without waiting for a prompt), as in the example below, or also list the current prompt in the block's prompts.
- On exit, the restore runs after the breakout. If the block leaves the session at a prompt that the restored prompts don't recognize (e.g. it has no breakout to leave the sub-CLI), the next `cmd` outside the block times out. A breakout that ends with `line: exit` leaves the session not at a prompt, and the next `cmd` waits for the outer prompt that the exit brings back.

A block breakout's handler reset (step 4) only restarts the response selection; it doesn't change the prompt state. Neither does the attach breakout's. A block without `prompts` doesn't swap, so its prompt state is never checked.

```yaml
- block:
    name: Install SONiC
    script:
      - cmd: sonic-installer install -y image.swi
```

With enter and breakout. The console is entered with `line`, not `cmd`: an idle console shows nothing until Return is pressed, and a wait that follows a `cmd` never sends that solicit newline, so `cmd: consutil connect 0` would time out (see [Prompt Handling](#prompt-handling-get_prompt)). After the `line`, the first `cmd` of the block waits for the console's prompt and presses Return if nothing shows within 5 seconds:

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

With block-scoped prompts (e.g. a sub-console with different prompt patterns):

```yaml
- block:
    name: Host Console
    prompts:
      - name: host-cli
        expect:
          - '^admin@host:~\$ ?$'
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

The value is required and must be an integer ≥ 1. As in JSON, a number with a zero fraction is that integer: `return: 2.0` sends two newlines. Any other number (`1.5`, `0`, `.inf`) is a validation error.

### `control` — Send control character(s)

```yaml
- control: "]"           # Ctrl+]
- control: [a, x]        # Ctrl+A then Ctrl+X
```

Each value is exactly one character: a letter `a`-`z` (Ctrl+A to Ctrl+Z; `A`-`Z` is the same) or one of ``@ ` [ { \ | ] } ^ ~ _ ?``. These are the keys that have a control character: `@` and `` ` `` send NUL, `[` and `{` ESC, `\` and `|` FS, `]` and `}` GS, `^` and `~` RS, `_` US, and `?` DEL. Anything else, such as `""`, `"ab"`, `"1"` or a non-ASCII letter, is a validation error (`control_char`) at the step's `control`, reported when the script is loaded: ``a control value is one character, a letter or one of @ ` [ { \ | ] } ^ ~ _ ?, got '<value>'``. An empty list is accepted and sends nothing. Quote the punctuation in YAML (`control: "]"`).

## Common Step Properties

All step types except `sleep` support:

| Field | Description |
|-------|-------------|
| `after` | Expect regex — wait for this pattern before executing. Matched against the raw output, escape sequences included (see [ANSI escape sequences](#ansi-escape-sequences)). On match, populates `session.before` and `session.match`. An invalid regex is a validation error (`invalid_regex`) or, for a template, a `ValueError` when it is rendered, before the wait (see [YAML Script Structure](#yaml-script-structure)). The pattern must not be empty: see below |
| `when` | Jinja2 conditional — template is rendered, step is skipped if the result is falsy (see below) |
| `delay_before` | Duration to wait before the step |
| `delay_after` | Duration to wait after the step |
| `timeout` | Duration that bounds each wait of this step (default 300s); see below |

`line` and `return` steps do not support `timeout`.

An empty `after` pattern would match at once, before any output, so the step would wait for nothing. It is treated like an empty `assert` pattern (see [`cmd`](#cmd--send-commands-to-the-shell)):
- `after: ''` is a validation error, in the schema and the models (`string_too_short` at the step's `after`: `an after pattern must not be empty: an empty regex matches at once, so the step would wait for nothing`), also on a plugin step.
- An `after` that renders to an empty string (e.g. `after: "{{ vars.p }}"` with `p: ""`) aborts the step with a `ValueError`, `after: the pattern rendered to an empty regex, which matches at once`, before anything is waited for or sent and before `when` is evaluated. Like an invalid regex it isn't a command failure, so `ignore_error` doesn't cover it.

To run a step without waiting, omit `after`. Only the empty string is rejected: `after: ' '` waits for a space.

The `after` wait comes before the step, and the step then runs as it would without it. `line`, `return` and `control` send on the match, since they never wait for a prompt. A `cmd`, an embedded script included, still makes its first prompt wait and sends at the prompt (see [`cmd`](#cmd--send-commands-to-the-shell)). `call` and `block` run their steps, each of which waits as its type does. The `after` wait never ends the at-a-shell-prompt state, and it starts it in one case: when the text it read ends with a shell prompt, with nothing read after it (see [Prompt Handling](#prompt-handling-get_prompt)).

`timeout` bounds each wait of the step separately; it isn't a deadline for the step as a whole, and it doesn't bound `delay_before` or `delay_after`. It applies to:
- `after`: the wait for the pattern, in every step type that has `after`. In `line` and `return`, which have no `timeout`, this wait uses the 300s default.
- `cmd`: also the wait for a prompt before the first line and after each line, the `$?` check, each wait of an embedded-script upload, and (capped at 10s) its cleanup.
- `call` and `block`: only the `after` wait. The steps of the function, or the block's `enter`, `script` and `breakout`, use their own `timeout`, or the 300s default; they don't inherit the `call` or `block` step's `timeout`.
- `control`: only the `after` wait.
- A plugin step: the `after` wait, and whatever the plugin does with the `timeout` it is passed.

`attach.timeout` bounds only the wait for the spawned process's first output; steps don't inherit it either.

These properties also apply to plugin steps. They are handled by the runner; the plugin's own model receives only its plugin-specific fields. Those fields are validated against the plugin's model when the script is loaded, wherever the step appears (the same places as for `call`). A failure is a validation error at the step's path, e.g. `script.0.ech0` with type `extra_forbidden` for a misspelled field.

A plugin step has exactly one plugin key. A step with keys of two or more registered plugins is a validation error of type `invalid_step` at the step's path, e.g. `script.0` with the message `step has more than one plugin key: free, loose` (the keys in the step's order), even when the first plugin's model allows extra fields. A built-in key takes precedence: in `{cmd: x, free: y}` the step is a `cmd` step and `free` is an unknown field (`extra_forbidden`).

Because the runner takes the common step properties out of every plugin step, a plugin can't use their names, and neither can it use a built-in step's key. A plugin is rejected when it is registered (at discovery: at the start of `autobot run` and `autobot schema`, and otherwise when a script needs a plugin or the `Runner` is created) if its key is a built-in step key (`cmd`, `sleep`, `call`, `block`, `line`, `return`, `control`) or a common step property name, or if its model has a field that a script could set through a common step property name: the field's name, its `alias`, or any of its `validation_alias` names. The error is a `PluginError` (from `autobot.registry`, a subclass of `TypeError`; `autobot.registry` is the module, so `from autobot.registry import PluginError` and `import autobot.registry as reg; reg.PluginError` both work, and the registry itself is `autobot.registry.registry`; its methods are reachable on the module too, so `from autobot import registry; registry.register(...)`, `registry.has(...)` and `registry.get(...)` work, while code that needs the object itself, e.g. for `isinstance(registry, StepRegistry)`, uses `autobot.registry.registry`) naming the plugin's class, with the distribution and entry point of a discovered plugin, its key and the fields, e.g. `plugin mypkg.TreeExecutor (distribution mypkg, entry point 'tree'), step key 'tree': model TreeStep reuses common step property names, which the runner handles and never passes to the plugin: timeout (field 'limit'), when`, or `plugin mypkg.CmdExecutor (distribution mypkg, entry point 'cmd'): step key 'cmd' is reserved (a built-in step or a common step property)`. A plugin registered in code, rather than discovered, is named by its class alone, e.g. `plugin mypkg.CmdExecutor: step key 'cmd' is reserved (a built-in step or a common step property)`. The key `plugin` is reserved too, and rejected the same way: `autobot schema` names a plugin's definition `<key>Step`, and `pluginStep` is the catch-all for unknown step keys (see [CLI](#cli)), and `plugin` is the step type that validation error locations show for every plugin step. Its message is `plugin mypkg.PluginExecutor: step key 'plugin' is reserved (the schema's pluginStep definition and the plugin step type in validation errors use the name)`. With it, no plugin key can produce the name of a definition of the static schema. Any other non-empty string is a valid key. Like a plugin that fails to import, the CLI reports it as `Plugin error: <message>` on stderr, without a traceback, and exits with status 1 before `attach.prepare` runs or anything is spawned (see [CLI](#cli)).

A plugin is also rejected at registration, before these checks, if its executor doesn't have the shape of a `StepExecutor` (`autobot.protocols`): a `key` attribute that is a non-empty string, a `model` attribute that is a pydantic model class (a `pydantic.BaseModel` subclass), and a callable `execute`. The error is a `PluginError` naming the plugin like the errors above and the first problem found, checked in that order, e.g. `plugin mypkg.BadExecutor (distribution mypkg, entry point 'bad'): executor has no 'model' attribute (a pydantic model class)`. The other messages are `executor has no 'key' attribute (a non-empty string)`, `executor's 'key' must be a non-empty string, got <repr>`, `executor's 'model' must be a pydantic model class (a pydantic.BaseModel subclass), got <repr>`, `executor has no 'execute' method` and `executor's 'execute' must be callable, got <repr>`. Reading an attribute that raises `AttributeError` counts as a missing attribute; one that raises any other exception (e.g. a `key` property that fails) is a `PluginError` chained from it, `executor's '<attribute>' attribute raised <type>: <message>` (just `<type>` when the exception has no message), e.g. `plugin mypkg.BadExecutor (distribution mypkg, entry point 'bad'): executor's 'key' attribute raised RuntimeError: key lookup failed`. `KeyboardInterrupt` and `SystemExit` are not wrapped. The registry is left as it was. `execute`'s signature isn't checked.

A step key belongs to one plugin. A plugin whose key is already registered by another plugin is rejected the same way, at registration, so a step is never dispatched to whichever plugin happened to be discovered last. The error is a `PluginError` naming both plugins, the new one first, with the distribution and entry point of each discovered one, e.g. `plugin pkg_b.EchoExecutor (distribution pkg-b, entry point 'echo'): step key 'echo' is already registered by plugin pkg_a.EchoExecutor (distribution pkg-a, entry point 'echo')`. The registry is left as it was. The CLI reports it like any other plugin error. Registering the same plugin again is not an error: the same executor instance is ignored, and another instance of the same class with the same model replaces it.

A plugin can't use the model of a built-in step. A built-in step is dispatched by the class of its model, so a plugin whose `model` is a built-in's would take over that step: with `key = "nap"` and `model = SleepStep`, every `sleep` step would run the plugin. Such a plugin is rejected at registration with a `PluginError`, e.g. `plugin mypkg.NapExecutor (distribution mypkg, entry point 'nap'), step key 'nap': model SleepStep is already the model of the built-in step 'sleep'; a plugin needs a model of its own`. This is checked after the reserved keys and before the common step property names, so a built-in's model is reported as that. `autobot.models.PluginStep`, the runner's model of every plugin step, is rejected the same way (`model PluginStep is the runner's own model of every plugin step; a plugin needs a model of its own`). A subclass of a built-in's model is a model of its own. It inherits that model's strict validation for the fields it adds: a value has exactly the field's type, so `count: 2.0` or `count: '2'` for an `int` field, and a value that isn't a list for a `list` field, are errors, where a model derived from `pydantic.BaseModel` itself converts them. Derive the model from `pydantic.BaseModel` to keep pydantic's usual conversions. The registry is left as it was, and the CLI reports it like any other plugin error. Two plugins may share a model with each other: a plugin step is dispatched by its key, not by its model, so with `nap` and `snooze` both using one model, a `nap` step runs the `nap` plugin and a `snooze` step the `snooze` plugin.

A plugin whose entry point raises while it is loaded (its module fails to import, the named attribute is missing, or creating the executor fails) is also a `PluginError`, `entry point '<name>' (distribution <dist>) failed to load: <type>: <message>` (just `<type>` when the exception has no message), chained from the original exception. `KeyboardInterrupt` and `SystemExit` are not wrapped. Discovery stops at the first plugin that fails; a broken plugin is never skipped, because a script or schema without it would fail or validate in a misleading way. A failed discovery isn't tried again: every later attempt in the same process (an explicit `discover()`, creating a `Runner`, or looking up a step key that isn't registered, as validating a script does) raises a `PluginError` with the same message, chained from the first one, so the plugins after the broken one are never silently missing. A `KeyboardInterrupt` or `SystemExit` during discovery isn't remembered; the next attempt loads the entry points again. The CLI reports a `PluginError` this way only from discovery: one raised while a script runs is an unexpected error, printed with its traceback (see [CLI](#cli)).

Order of evaluation: `after` (wait) -> `when` (decide) -> `delay_before` -> execute -> `delay_after`.

### Conditional execution with `when`

The `when` field accepts a Jinja2 template string. The rendered result is evaluated as a boolean gate: it is falsy if, with surrounding whitespace removed and lowercased, it is `""`, `"false"`, `"0"` or `"none"`. A falsy result skips the step entirely. So `False` (how Jinja2 renders a boolean, e.g. `{{ vars.flag }}` or `{{ a == b }}`), `FALSE`, `None` (how Jinja2 renders a null value, e.g. `{{ vars.v }}` when `v` is `null`) and `" false "` are all falsy. Any other result, including `"no"` and `"off"`, runs the step. `when` is a condition, not text that is sent or stored, so a boolean result is exactly what it is for: it is the one templated value where an expression may give a boolean (see [Booleans are not text](#booleans-are-not-text)).

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

A bare number must be finite and ≥ 0: YAML's `.nan`, `.inf` and `-.inf` are rejected when the script is loaded, so there is no "wait forever" duration. Finite means at most the largest double, 1.7976931348623157e308 (the schema's `maximum`); a larger integer, such as one of 400 digits, is rejected too. A boolean is not a duration, and neither is `null` (so `sleep:` with no value is rejected). A string must be a non-negative number immediately followed by one of the units `ms`, `s`, `m`, `h`, with nothing else (`"5"`, `"1 s"` and `"-1s"` are rejected). The number is written in ASCII digits `0`-`9` only (`"٥s"` is rejected), and a string whose value overflows to infinity is rejected too.

## Jinja2 Templating

These values are rendered as Jinja2 templates:
- `cmd` (a string, or each item of a list, as a whole before it is split into lines; an embedded script as a whole), `assert`, `line`, `after` and `when`
- a prompt's `send` string, each time it is sent (the values `sendEach` takes from `vars` are sent as they are)
- `attach.spawn` and `attach.prepare`
- `env` values (see [Top-level fields](#top-level-fields))

Other values are used verbatim, e.g. `expect`, `errors`, `control`, `call`, `register` and `attach.env`. A plugin step decides which of its own fields it renders.

Any template error in any templated value, a syntax error or an undefined variable, is reported as a `ValueError` (a `ScriptError`, see [CLI](#cli)) with the message `template error: ...`. So is any other exception an expression raises while the value is rendered: the message then names the exception's type, `template error: <type>: <message>` (just `<type>` when the exception has no message), e.g. `template error: ZeroDivisionError: division by zero` for `{{ 1/0 }}`, or `template error: TypeError: can only concatenate str (not "int") to str` for `{{ 'a' + 1 }}`. The `ValueError` is chained from the original exception. An error that is already reported is not wrapped again: a template error from a render inside the render (an `env` default referenced by another, a failing `search` regex) keeps its message, and an `env` cycle or nesting error stays `env cycle: ...` (see [Top-level fields](#top-level-fields)). `KeyboardInterrupt` and `SystemExit` are not wrapped. This includes plugin fields rendered through the runner. It always aborts the step, and with it the script, even with `ignore_error: true`. In a breakout it ends that breakout and is logged, like any other breakout error.

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
| `env` | The environment: every variable Autobot was started with, and the defaults of the YAML's `env` section for the variables it doesn't set (see [The environment in templates](#the-environment-in-templates)) |
| `vars` | `vars` section of the YAML (also populated at runtime by `cmd` steps with `register`) |
| `args` | CLI `--arg KEY=VALUE` arguments |
| `session.before` | Text captured before the last `after` match (pexpect `before`), or the captured output of the last command when a shell prompt is reached (empty if it printed nothing). The `$?` check, embedded-script cleanup and the first prompt wait of a `cmd` with `after` don't change it. |
| `session.match` | Text that matched the last `after` pattern (pexpect `after`), or the text the prompt regex matched when a shell prompt is reached. After an `after` match, `session.before` and `session.match` are raw text; after a shell prompt they have ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)). |

`env`, `vars`, `args` and `session` are mappings. On a mapping, `x.name` and `x['name']` both read the key `name`, and with `x.name` a key always wins over a mapping method of the same name: with `register: values`, `{{ vars.values }}` is the registered output, not the `values` method (plain Jinja2 would render `<built-in method values of dict object ...>`). The same goes for `items`, `keys`, `get`, `copy` and the rest, and for mappings nested in `vars` (`vars.site.values`). A method is reachable as `x.name` only while there is no key of that name, so `vars.items()`, `vars.get('k', 'default')` and `env.get('KEY')` work as long as no key is named `items` or `get`. Filters don't depend on key names: `vars | items`, `vars | length`, `vars | tojson`.

Built-in global: `range`. Use Jinja2 filters for other operations (e.g. `{{ items | length }}`).

### The environment in templates

`env` holds one value for each variable name, taken from the first of these that has it:

1. the environment Autobot was started with,
2. the default of that name in the YAML's `env` section (see [Top-level fields](#top-level-fields)).

So `{{ env.HOME }}`, `{{ env['SSH_AUTH_SOCK'] }}` and `{{ env.get('HTTPS_PROXY', '') }}` work without an `env` section, and a key of the `env` section is needed only to give a variable a default. A variable that is set to an empty string is set: `env.X` is `''`, and its default isn't used. The environment is read once, when the run starts.

A variable that neither has is not in `env`. `{{ env.KEY }}` and `{{ env['KEY'] }}` are then an undefined variable, a template error with the message `template error: env has no key 'KEY'`; `{{ env.KEY | default('x') }}`, `env.get('KEY', 'x')`, `'KEY' in env` and `env.KEY is defined` deal with it as they do on any mapping.

`env` is one mapping of all these variables, and its methods and the filters see all of them: `env | length` counts them, `env | items`, `env.keys()` and `{% for name in env %}` go through them, and `{{ env | tojson }}` writes them all out, values included. The keys of the `env` section come first, in the order written, whether the environment sets them or not; the other variables follow, sorted by name. Every value is a string. A value of the environment that isn't valid UTF-8 keeps its other bytes as lone surrogates, as Python's `os.environ` reads it; text with one can't be sent to the session (a `UnicodeError`, see [CLI](#cli)).

### Booleans are not text

A template is rendered to text that the engine sends or stores, and a boolean is not text: there is no one spelling for it (`true`, `True`, `yes`, `on`, `1`), and the device decides which it accepts. So an expression whose value is a boolean is a template error: `cmd: "set debug {{ vars.debug }}"` with `debug: true`, `{{ a == b }}`, `{{ x is defined }}`, `{{ value | contains('x') }}`. The message is `template error: an expression gave a boolean (true), which is never written as text; quote the value in the script, or say which text is meant, e.g. {{ value | string }} (True) or {{ value | tojson }} (true)`, with `(false)` for a false value. Like any [template error](#jinja2-templating) it aborts the step, also with `ignore_error: true`, and nothing of that value is sent. To use a boolean-looking value as text:
- Quote it in the script, so that it is a string: `debug: 'true'`, `debug: 'yes'`. `{{ vars.debug }}` then renders what was written.
- Or say in the template which text is meant: `{{ vars.debug | string }}` is `True` or `False`, `{{ vars.debug | tojson }}` (or `| string | lower`) is `true` or `false`, and `{{ 'on' if vars.debug else 'off' }}` is whatever the device wants.

The rule is about the value a `{{ ... }}` writes out, and only when that value is exactly a boolean:
- A boolean used inside a template is unaffected: `{% if vars.debug %}`, `{{ 'on' if vars.debug else 'off' }}`, `vars.debug == true`, `x is defined` in an `{% if %}`, and filters and tests inside an expression all work on the value.
- Text an expression builds itself is what Python makes of it, with no error: `{{ vars.debug ~ '' }}`, `{{ '%s' | format(vars.debug) }}` and `{{ [vars.debug] | join(',') }}` give `True`. So is a boolean inside a list or a mapping that is written out whole: `{{ vars.flags }}` with `flags: [true, false]` is `[True, False]`; `{{ vars.flags | tojson }}` is `[true, false]`.
- A string and a number are never refused: `word: 'True'` renders `True`, and `1` and `0` render as they are.
- A null is not a boolean: it is written `None` (`{{ vars.v }}` when `v` is `null`), as Jinja2 does it.
- `when` is a condition and is rendered without this rule (see [Conditional execution with `when`](#conditional-execution-with-when)). A plugin renders a condition of its own with `ctx.render(template, condition=True)`.

The engine doesn't turn a boolean of the document into text anywhere else either:
- Every value of the script that is text is a string, and a YAML boolean there is a validation error when the script is loaded (`string_type`, in the schema and the models): `cmd: yes`, `line: on`, `register: true`, `when: true`, `attach.spawn`, a prompt's `name` or `expect`, and so on. A prompt's `send` has its own error with a quoting hint (`send_type`, see [`prompts`](#prompts)).
- A value of `env` or `attach.env` is a string. A boolean there (`DEBUG: true`, `NO_COLOR: yes`) is `string_type` too, and the models say what to do: `an environment value is a string, and unquoted this one is a boolean (true); quote it to set it as written, e.g. 'true' or 'yes'`.
- A `sendEach` refuses a boolean item or field value when the prompts are loaded (see [`sendEach`](#sendeach)).
- `--arg` values are always strings: `--arg debug=true` is the text `true`.

A boolean stays a boolean where that is what is meant: `ignore_error`, a prompt's `return`, and any value in `vars`, which templates use in conditions. Numbers are not concerned by any of this: a number in `vars` renders as its digits, and a number where a string goes is `string_type`, with pydantic's own message.

An error message that shows a boolean of the document shows it as YAML writes it: `unsupported autobot version 'true'`, `invalid duration: true`, `a boolean (true)`. YAML reads `yes`, `on` and `True` as the same value, so the message can't show which of them was written.

The `contains` and `search` filters read the value they look in as `str()` gives it, which matters only for a boolean passed to them directly (`{{ vars.debug | contains('True') }}`).

### Custom Filters

| Filter | Usage | Description |
|--------|-------|-------------|
| `contains` | `{{ value \| contains('substring') }}` | True if `substring` is found in `value`. The result is a boolean: use it in a `when` or an `{% if %}`, not as text (see [Booleans are not text](#booleans-are-not-text)) |
| `search` | `{{ value \| search('regex') }}` | True if the regex pattern matches anywhere in `value`. An invalid regex is a template error (`template error: search: invalid regex ...`) |

## Prompt Handling (`get_prompt`)

`get_prompt()` only detects and navigates to a shell prompt — it does not send commands. The caller is responsible for sending the command via `sendline()` after `get_prompt()` returns.

The session is *at a shell prompt* from the moment a prompt wait ends at one, or an `after` wait reads text that ends with one (the prompt's match ends where the text read ends, and nothing more has been read), until anything is sent (a command line, the `$?` check, `line`, `return`, `control`) or the session is closed. A closed session is at no prompt: a prompt wait on it fails with `RuntimeError` (`not attached`), like every other use of it. Nothing of a closed session applies to a process spawned after it: the first wait on the new process follows no command, so it solicits, the echo of a line sent to the old one isn't looked for, and `session.before` and `session.match` start empty. A prompt wait while the session is at a shell prompt returns at once, without reading output, sending anything, or changing `session.before` and `session.match`. A block's prompt swap and restore can end this state but never start it (see [Prompt state across a swap](#prompt-state-across-a-swap)).

While it waits, the engine also consumes each `\r\n` and each ANSI escape sequence as it arrives: the text before it goes to the captured output, and the escape sequence itself is dropped (see [ANSI escape sequences](#ansi-escape-sequences)). It also discards stray control characters at the very start of the unread output: a run of carriage returns (`\r`), NULs (`\x00`) and BELs (`\x07`) that is followed by any character other than these and a line break. Some devices and console servers print them before a prompt (a `\r` to return the cursor, NUL padding, a bell). The run is discarded only once the character after it has arrived, so a `\r` whose `\n` comes in a later read is still the line break `\r\n`, and a run that is followed by a line break is not discarded as stray: it is part of that line. Its NULs and BELs are captured (`\x00\r\n` is `\x00\n`), and its carriage returns go with the line break, like any carriage returns that end a line (`\r\r\n` is an empty line, `out\r\r\n` is `out\n`). Only the start of the unread output is concerned, that is, what follows a line break, an escape sequence, an answered prompt or whatever the previous wait consumed: these characters anywhere else, such as the `\r` of a line that rewrites itself (`10%\r50%\r100%`), are captured as they are, except for carriage returns right before a line break. When several patterns match, the one that starts earliest in the unread output wins; on a tie, `\r\n` comes first, then an escape sequence, then the prompts in the order they are defined, and the stray characters last (so a prompt regex that itself starts with `\r` still matches there).

A bare `\n` without `\r` is not a line break for the engine and is not consumed. A pty turns every `\n` a program writes into `\r\n`, so this only concerns a remote that has turned that off or a raw line that sends `\n` alone: there a prompt that follows output is not at the start of the unread output, a regex anchored with `^` doesn't match it, and the prompt regex must be left unanchored.

Prompt regexes (`expect`, and the `match` regexes of `sendEach` `fields` entries) are compiled with `re.DOTALL` and searched for anywhere in the unread output; they aren't anchored at its start. There is no `re.MULTILINE`, so `.` matches line breaks and `$` matches only at the end of the unread output (or before a final `\n`). A shell prompt regex should end at the prompt character (e.g. `'^\w+@[\w.-]+:[^\r\n]*[$#] ?$'`, the regex of `examples/sonic.autobot.yaml` for `admin@sonic:~$ `): any part of the prompt it leaves unmatched stays in the stream and becomes the start of the next command's captured output, where the echo of the command is then no longer recognized and removed. It should also describe the whole prompt, not just its last character: because the search isn't anchored, a regex such as `[>#$] ?$` matches any output that happens to end a read with `>`, `#` or `$`, and the command that follows is sent into whatever is still running. A regex that describes the whole prompt can still be found inside output: without the `^`, the one above matches `scp admin@host:/x $` when that ends a read. So start the regex with `^`, as both examples do: it anchors the prompt to the start of its line. `^` matches at the start of the unread output, and that is the start of a line whenever a prompt is due, because the engine has consumed the line breaks and escape sequences before it and discarded any stray `\r`, NUL or BEL in front of it. An anchored regex doesn't match a prompt that has other text before it on its line: one printed right after output with no final newline (`printf foo`), or one that follows a `\r` in the middle of a line (text, `\r`, then the prompt). Leave the regex unanchored for a device that does that.

The prompt engine polls the session output in 5-second intervals:
1. If a prompt with no `send` (or `return: true`) matches → return (shell prompt reached)
2. If a prompt with `send` values matches → send the response chosen as described in [Response selection](#response-selection) and continue waiting
3. On 5-second timeout with no match → send a single empty newline to solicit a prompt (once only, only if no handler has been activated yet, and never when the wait follows a command; see below)
4. On overall timeout → raise `TimeoutError` (`timed out after <timeout>s waiting for a shell prompt ('<name>', ...)`, naming the current prompts that are shell prompts, or `none defined`)
5. If the connection closes (the process exits) → raise `EOFError` at once (`connection closed while waiting for a shell prompt ('<name>', ...)`, with the same names)

This handles idle consoles that need a return press to display a prompt.

The solicit newline is for a console that is idle, not for a command that is still running. A wait that follows a command never solicits, however long the command stays silent: the wait after a `cmd` line, after the `$?` check, after each line of an embedded-script upload and its cleanup `rm`, and after a plugin's `session.sendline(...)`. A shell would answer the newline with a second prompt once the command ends, and that stale prompt would end the next wait early, so each later step would capture the output of the command before it. A wait solicits when the last thing sent before it wasn't a command: nothing at all (the first wait after the spawn, a wait after a prompt swap, or the wait after one that timed out), or a raw send, which doesn't wait for a prompt itself (`line`, `return`, `control`, the `^C` of an embedded-script cleanup). The one exception is the first prompt wait of a `cmd` with `after`: it follows output, not silence, and never solicits (see [`cmd`](#cmd--send-commands-to-the-shell)). So a `cmd` after `line: consutil connect 0` still gets the return press an idle console needs, while `cmd: consutil connect 0` itself would wait for a prompt that never comes: start anything that shows nothing until Return is pressed with `line`. A `$?` check that fails before its result arrives (a timeout or a closed connection) counts like a prompt wait that timed out: the next wait solicits. A plugin's `session.sendline(text)` is a command; a plugin marks a raw send, one whose prompt it doesn't wait for, with `session.sendline(text, solicit=True)`.

## CLI

```
autobot [run] <script.yaml> [-a KEY=VALUE ...] [--traceback]
autobot schema [--traceback]
autobot -h | --help
```

Subcommands:
- `run <script>`: load, validate and execute the script. `script` is the path to the YAML script file.
- `schema`: print the JSON schema to stdout (see below). It takes no arguments, and one option, `--traceback`.

`run` is the default. If the first argument isn't `run`, `schema`, `-h` or `--help`, the CLI treats the command line as `autobot run ...`, so `autobot <script>` is the same as `autobot run <script>`, and options may come before the script (`autobot -a k=v <script>`). A script file named `run` or `schema` must be given with the subcommand (`autobot run schema`) or as a path (`autobot ./schema`).

Options of `run`:

| Flag | Description |
|------|-------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | Pass an argument to the script, accessible as `{{ args.KEY }}`. Repeatable, one `KEY=VALUE` per flag. The value is everything after the first `=`, so it may contain `=`. Values are strings. If a key is given more than once, the last value wins. |
| `--traceback` | Also print the Python traceback of an error that is reported without one (see [Errors while the script runs](#errors-while-the-script-runs)). |
| `-h`, `--help` | Print the `run` usage and exit with status 0. |

`autobot -h` prints the list of subcommands and exits with status 0. `autobot` with no arguments prints the same help to stdout and exits with status 1.

`autobot schema` prints the `2026-10` JSON schema, indented, to stdout, extended with the step types of installed plugins. It first loads the plugins registered in the `autobot.steps` entry-point group, as `run` does, so a plugin that can't be loaded is reported (`Plugin error: ...`) before anything is printed. Then it reads the schema that ships with the package: `autobot/autobot.2026-10.json`, a copy of `schemas/autobot.2026-10.json` that every wheel and source distribution contains. The command never uses the network, and an installed autobot prints the schema of its own version. The repository keeps one copy of the schema, in `schemas/`; the file in the package directory is a link to it, which a build replaces with the file. In a source tree where the file in the package directory is missing or isn't the schema, the command reads `schemas/autobot.2026-10.json` directly; a checkout made without symbolic links has a small text file there that holds the link's target. If the schema is in neither place, the CLI prints `Cannot read the schema: neither <packaged file> nor <schemas file> holds the JSON schema` on stderr and exits with status 1, without a traceback. A wheel must be built from a tree where the link is a link: built from a checkout without symbolic links it would ship that text file, and its `autobot schema` would fail this way. For each plugin it adds `$defs.<key>Step`, and a `$ref` to it in `$defs.step.oneOf` just before the final `pluginStep` entry. `<key>Step` requires the plugin's key and combines the [common step properties](#common-step-properties), from the static schema's `$defs.stepCommon`, with the plugin model's pydantic JSON schema, which describes the plugin's own fields. Any other key is rejected, unless the plugin's model allows extra fields (the model's `additionalProperties` becomes the definition's `unevaluatedProperties`). Definitions nested in the plugin's schema stay under `$defs.<key>Step.$defs`. The definition is named with the key as it is. In the `$ref`s that point to it or into it, the name is escaped as a JSON pointer in a URI fragment requires (`~` as `~0`, `/` as `~1`, and other characters outside letters, digits and `-._~` percent-encoded), so a key such as `a/b` or `a b` gets a working definition (`#/$defs/a~1bStep`, `#/$defs/a%20bStep`). When the model refers to itself, its pydantic schema is a `$ref` to its own definition; a copy of that definition takes the `$ref`'s place in `<key>Step`, so the step accepts the common step properties, and the definition stays under `$defs.<key>Step.$defs` for the nested references, which accept only the model's own fields. The plugin's key is also added to the keys `pluginStep` excludes, so a step with that key matches only `<key>Step`: it is checked against the plugin's model and the common step properties, and an invalid one is rejected rather than accepted as an unknown plugin step. A step with a key no installed plugin registers still matches `pluginStep`, which applies `stepCommon` in both the static and the generated schema: its common step properties are checked, and its other keys are not.

A run that completes prints `>> run completed` as its last line on stderr and exits with status 0.

The script file is read as a single YAML document, encoded as UTF-8 (or UTF-16 with a byte order mark). Keys must be unique in every mapping at every level (top level, `attach`, `env`, `vars`, `fn`, prompts, steps, and any nested value). A repeated key is an error rather than overriding the earlier value. Keys are compared as loaded, so `x` and `"x"` are the same key, while `1` (an integer) and `"1"` (a string) are different keys. A key set next to a `<<` merge key overrides the merged value and isn't a duplicate. A mapping can have only one `<<` key; to merge several mappings, use `<<: [*a, *b]`. The CLI reports these load errors on stderr without a traceback:

| Error | First line |
|-------|------------|
| An installed plugin can't be loaded: its entry point fails to import or to create the executor, or the plugin is rejected at registration (an executor without a usable `key`, `model` or `execute`, a reserved key or common-name field, a key another plugin registered, or the model of a built-in step; see [Common Step Properties](#common-step-properties)). Also reported by `autobot schema` | `Plugin error: <message>`, e.g. `Plugin error: entry point 'echo' (distribution pkg-b) failed to load: ModuleNotFoundError: No module named 'foo'` or `Plugin error: plugin pkg_b.EchoExecutor (distribution pkg-b, entry point 'echo'): step key 'echo' is already registered by plugin pkg_a.EchoExecutor (distribution pkg-a, entry point 'echo')` |
| The file can't be read (missing, a directory, permission denied) | `Cannot read script <path>: <reason>` |
| The file isn't valid YAML (syntax error, tab indentation, more than one document, an undefined alias, an unsupported tag such as `!!python/object`, a duplicate key) | `YAML error in <path>, line <L>, column <C>: <problem>`, followed by an indented context line when YAML gives one. For a duplicate key the problem is `found duplicate key '<key>'` at the repeated key, and the context line is `  first defined (line <L>, column <C>)` |
| The file has bytes that aren't valid UTF-8, or disallowed control characters | `YAML error in <path>, position <N>: <reason> (...)` |
| The script fails validation, including an empty file, a document that isn't a mapping, an undefined `call` target or invalid plugin step fields | `Validation errors:`, followed by one line for each error (see below) |
| An `--arg` has no `=` | `--arg requires KEY=VALUE format, got: <arg>` |
| The top-level `env` can't be resolved (a template error, a reference cycle, or nesting more than 50 keys deep), a top-level prompt `send` string has a template syntax error, or a top-level prompt's `sendEach` collection can't be resolved (see [`sendEach`](#sendeach)) | `Script error in <path>: <message>`, e.g. `Script error in <path>: template error: ...`, `Script error in <path>: env cycle: A -> B -> A` or `Script error in <path>: prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'` |

A validation report is the line `Validation errors:` and then one line for each error, in the order the models report them:

```
Validation errors:
  autobot: autobot 2026-08 is no longer supported; use 2026-10 [unsupported_version]
  attach.timeout: invalid duration: 5 minutes [value_error]
  script.0.cmd.timout: Extra inputs are not permitted [extra_forbidden]
  script.1: cannot determine step type; expected one of cmd, sleep, call, block, line, return, control or a registered plugin step (got a mapping with the key cmdd) [invalid_step]
  script.2.block.block.script.1.return.return: Input should be greater than or equal to 1 (got 0) [greater_than_equal]
```

A line is two spaces, then `<location>: <message>`, then ` (got <value>)` where it applies, then ` [<type>]`:
- The location is the error's location as the models report it, its keys and indexes joined with `.` (a step's type tag included, as in `script.0.cmd.timout`). An error of the document as a whole, such as a file that is empty or holds a list, is at `(document)`.
- The message is the error's message; pydantic's `Value error, ` prefix is left out.
- The value is the offending value as YAML writes it: a string in quotes, a number, `true`, `false` or `null`. A mapping is shown as `a mapping with the keys <key>, ...` (`an empty mapping`), a list as `a list of <N> items`, and any other value by its YAML type (`a timestamp`, `binary data`). What is shown is cut after 60 characters with `...`. No value is shown when the message already shows it (in quotes, at its end, in parentheses for a boolean or number, or anywhere for a string of more than three characters), and none for a `missing` or `extra_forbidden` error, where the location says it all.
- The type is the error's type, as named throughout this document (`invalid_regex`, `string_type`, ...).

Each error is one line, however long its message: the report is never wrapped. On a terminal the location is bold and the type dim (see [Output](#output)).

Line and column numbers start at 1; `position` is a 0-based offset into the file. With `--traceback`, a `Plugin error` and a `Script error` are preceded by the Python traceback of the exception they report. For each of these errors the CLI exits with status 1, and nothing runs: `attach.prepare` isn't run and no session is spawned. Only the first error is reported. The installed plugins are loaded first, because validation depends on them, so a broken plugin is reported even when the script itself has an error. Then the file is read and parsed, then validated, then `--arg` values are checked, then `env` and `prompts` (after `--arg`, because `env` may use `{{ args.KEY }}`). Command-line syntax errors caught by the argument parser, such as `--arg` with no value, `run` without a script, an unknown option, or an argument to `schema`, print usage and exit with status 2.

### Output

Autobot writes to two streams, and each carries one kind of text:

| Stream | Carries |
|--------|---------|
| stdout | The session's output: everything read from the spawned process, with ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)). During a run nothing else is written to it. `autobot schema` and the help also print to stdout. |
| stderr | Autobot's own messages: the `>> ` progress lines, the CLI's error reports and tracebacks. |

So `autobot script.yaml > device.log` keeps the device's transcript and shows what Autobot did, and `2> run.log` does the opposite. The output of `attach.prepare` is the script's own: it inherits both streams.

When stdout and stderr are the same terminal, pipe or file (a terminal with nothing redirected, `2>&1`, `&> run.log`), their lines interleave, and the session's output seldom ends with a line break: a prompt such as `admin@sonic:~$ ` leaves its line open. A message would then continue that line. So where the two streams are one and the session's output has left a line open, Autobot writes a line break to stderr before the message, and every message starts at the first column:

```
admin@sonic:~$ 
>> cmd: show version
show version
SONiC Software Version: ...
```

The line break goes to stderr, never to stdout, and none is written when the two streams are different or the session's output ended its line. So stdout is the same transcript however the streams are set up.

**The session's output is never restyled.** It is written as the device sent it, less the escape sequences: not wrapped, cut, reflowed or highlighted. Text in it that looks like markup or an emoji code (`[bold]`, `[/]`, `:warning:`) is printed as it is, and no style is ever added to it.

**A progress line** starts with the marker `>> `, so the lines are told apart from the device's in a plain log (`grep '^>> '`). After the marker comes a label, which ends at the first `: `, and the rest of the text. These are the progress lines:

| Line | Kind | Printed |
|------|------|---------|
| `>> prepare: running local script`, `>> prepare: running local script (no shebang, using /bin/sh)` | step | before `attach.prepare` runs |
| `>> attach: <spawn>` | step | before the process is spawned, with the rendered command line |
| `>> cmd: <line>` | step | for each line a `cmd` sends, as it is sent |
| `>> sleep: <seconds>s` | step | by a `sleep` step |
| `>> control sent: ^<C>` | step | for each character a `control` step sends |
| `>> line sent` | step | for each line a `line` step sends, without its text |
| `>> return sent` | step | for each newline a `return` step sends |
| `>> prompt answered: <name>` | step | each time a prompt with `send` is answered during a prompt wait, with the prompt's name and without the response |
| `>> block enter: <name>`, `>> block breakout: <name>`, `>> call: <function>`, `>> breakout: detaching` | group | when a block starts, when its breakout starts, when a `call` starts the function's steps, when `attach.breakout` starts |
| `>> prepare: done`, `>> block completed: <name>`, `>> run completed` | completed | when `attach.prepare` or a block has finished without an error; `run completed` is the CLI's last line of a run that completed |
| `>> register: vars.<name>`, `>> script: writing to <file>`, `>> script: executing <file>`, `>> script: cleaned up <file>` | detail | by `register`, and by an embedded script |
| `>> error ignored: <message>`, `>> breakout error (<type>): <message>`, `>> block breakout error (<type>): <message>`, `>> close error (<type>): <message>`, `>> script: interrupt sent: ^C`, `>> script: cleanup of <file> failed (<type>): <message>` | warning | for a failure the run goes on from; `<type>` is the exception's class name |
| `>> step failed (<type>): <message>`, `>> step interrupted` | failure | by the step that ends with an error, as it fails: an error that the step doesn't ignore, or an interrupt. It is printed once, by the innermost step, at that step's level and before any breakout runs. A failing step of a breakout prints it too, before the breakout's own `breakout error` line |

**Nesting.** The steps of a block and of a called function are indented under the line that starts them, two spaces a level. The indentation comes after the marker, which stays in the first column:

```
>> block enter: Host Console
>>   cmd: show version
>>   call: is_system_running
>>     cmd: systemctl is-system-running --wait
>> block breakout: Host Console
>>   cmd: logout
>> block completed: Host Console
```

A block's own lines (`block enter`, `block breakout`, `block completed`) and a `call:` line are at the level of the block or `call` step itself, and so is every other line a step prints (`error ignored`, `register`, `script: ...`). The steps of `attach.script`, `script` and `attach.breakout` are at the top level, as are the lines before and after them (`prepare`, `attach`, `breakout: detaching`, `close error`). The steps a plugin step runs with `ctx.run_steps(...)` are one level below it. A function has no line for its end: the next line at the level of its `call:` line, or above, is not part of it. The table above shows every line as at the top level. The session's output is never indented, and neither are the CLI's reports.

Autobot says that a `line` step sent a line and that a prompt was answered, never what was sent: the text may be a password, and it shows only as far as the device echoes it. That goes for a `send` string and for the values of a `sendEach` alike. A `cmd` line is printed, since a command is echoed by the device anyway. The newline that a prompt wait sends on its own to an idle console (see [Prompt Handling](#prompt-handling-get_prompt)) prints no line.

**The CLI's reports** have no marker: the load errors, `Run failed in ...`, `Interrupted` and `Unexpected error in ...` (see below). They start at the first column with what happened, then `: ` and the message.

A message is printed as it is. Nothing in it is read as markup or as an emoji code, and numbers, quoted strings and paths get no highlighting. Only a few control characters are not passed on: a tab is written as spaces, up to the next multiple of eight columns, and a BEL, backspace, vertical tab, form feed or carriage return is left out. Autobot adds no line break to a message and never cuts it, whatever the width of the terminal.

**Styles.** On a terminal the messages are styled; the words are the same with and without styles, and no meaning is carried by a style alone.

| What | Style |
|------|-------|
| step | marker bold blue, label bold, the rest plain |
| group | marker bold blue, the whole text bold |
| completed | marker bold green, text green |
| detail | marker and text dim |
| warning | marker bold yellow, label yellow, the rest plain |
| failure | marker bold red, label red, the rest plain |
| a report's first words, up to the `: ` | bold red; `Interrupted` is bold yellow |
| a validation error's location, and its type | bold, and dim |
| the `at` and `called from` labels of a report | dim |

Only bold, dim and four of the terminal's own eight colors are used (SGR 1, 2 and 31 to 34: red, green, yellow and blue), never a fixed RGB value or a background color, so the terminal's theme decides the exact colors and keeps them readable on a dark and on a light background.

The messages are styled when stderr is a terminal whose `TERM` isn't `dumb` or `unknown`. Otherwise they are plain text without any escape sequence, so a pipe or a file gets a clean log. Two environment variables change that, each when set to a non-empty value:
- `NO_COLOR`: no escape sequences at all, bold and dim included. It wins over `FORCE_COLOR`.
- `FORCE_COLOR`: styles even when stderr isn't a terminal, and for a `dumb` terminal. Any non-empty value counts, `0` too.

The session's output on stdout has no styles in any of these cases.

### Errors while the script runs

Once the script is loaded, the run itself can fail or be interrupted. For an error after the spawn wait succeeds, breakouts still run and the session is closed first, as the [`attach`](#attach) and [`block`](#block--named-group-of-steps) lifecycles describe. When the spawn wait itself fails, no breakout runs; the process is closed first (see [`attach`](#attach)). Then the CLI reports the error on stderr and exits. It tells three kinds of error apart.

**A failed run** is a problem of the script, the device or the environment: something the user can act on. It is reported without a traceback, and the CLI exits with status 3:

```
Run failed in <path>: <reason>
  at <step path> (<step>)
  called from <step path> (<step>)
```

`<path>` is the script file as given on the command line, and `<reason>` is the error's message. The step that failed has said so already, where it failed in the log (`>> step failed (<type>): <message>`, see [Output](#output)); the report comes last, after the breakouts. These are the failed runs:

| Failure | Exception | `<reason>`, e.g. |
|---------|-----------|------------------|
| A command failure that `ignore_error` doesn't cover: a non-zero exit code, an `assert` where no pattern matches, an embedded-script upload mismatch | `StepFailure` (`autobot.steps`), a `RunError` | `command returned exit code 1`, `assertion failed: expected ['version 5\\.']` |
| An `errors` pattern match | `CommandError` (`autobot.session`), a `RunError` | `command error: % Invalid input` |
| A `sendEach` prompt out of responses | `RunError` | `prompt 'login': responses exhausted` |
| A failed `attach.prepare` | `RunError` | `prepare script failed with exit code 3` |
| A template error at run time, an `assert` or `after` that renders to an invalid or empty regex, an `attach.spawn` that renders to no command, a block's `sendEach` collection that can't be resolved | `ScriptError` | `template error: 'dict object' has no attribute 'image'` |
| A timeout: waiting for a prompt, an `after` pattern, the `$?` result or the spawned process's first output | `TimeoutError` | `timed out after 30.0s waiting for a shell prompt ('sh')` |
| A closed connection | `EOFError` | `connection closed while waiting for a shell prompt ('sh')` |
| A `spawn` command that isn't found, or a process that can't be terminated when the session is closed | `pexpect.ExceptionPexpect` | `The command was not found or was not executable: sssh.` |
| An operating-system error on the session's pty or on the temp file of the `prepare` script, e.g. a full disk | `OSError` | `[Errno 28] No space left on device` |
| Text that can't be encoded for the session or the `prepare` script, e.g. the lone surrogate a non-UTF-8 byte of an `--arg` becomes | `UnicodeError` | `'utf-8' codec can't encode character '\udcff' in position 5: surrogates not allowed` |
| Functions that call each other without end | `RecursionError` | `functions call each other too deeply (maximum recursion depth exceeded)` |

`RunError` and `ScriptError` are in `autobot.types`. `RunError` is a `RuntimeError` and `ScriptError` a `ValueError`, so code that catches those catches them too; `EnvError` is a `ScriptError`. Where this document says an error is a `ValueError` with a `template error: ...`, `assert: ...`, `after: ...`, `attach.spawn ...` or `prompt '<name>': sendEach ...` message, it is a `ScriptError`; the `RuntimeError` of a `sendEach` prompt (`responses exhausted`, `no response available`) and of a failed `prepare` is a `RunError`. A message that has no text is reported as the exception's type name. A plugin reports a failure of this kind by raising one of these exceptions, e.g. `StepFailure` or `RunError` for what the device did and `ScriptError` for a bad value in the script.

The `at` line names the step that was running: its path in the script, and in parentheses what it is.
- The path is the list the step is in and its index, counted from 0. The lists are `script`, `attach.script`, `attach.breakout`, `fn.<name>.script`, and for the block at `<path>`, `<path>.block.enter`, `<path>.block.script` and `<path>.block.breakout`. So the second step of the main script of a block that is the fourth step of `script` is `script.3.block.script.1`. The steps a plugin builds in code and runs with `ctx.run_steps(...)` are in no list of the script: they are named after the plugin step that runs them, `<plugin step's path>.<key>.<index>`.
- A `cmd` shows the first non-blank line of the command as written in the script, not rendered, cut after 72 characters with `...`; for a list, the first item and ` (+<N> more)`. A `call` shows the function's name and a `block` the block's name, e.g. `(call: is_system_running)`, `(block: Host Console)`. Every other step shows only its key: `(line)`, `(control)`, `(sleep)`, `(return)`, and a plugin step its plugin key. A `line` never shows its text: it may be a password, which the session doesn't echo.
- A failure outside any step has no `at` line: a failed `prepare`, a failed spawn wait, a `spawn` that renders to no command, a session that can't be closed.

A `called from` line follows for each step that was running the failing one through a `call` (or a plugin step that runs steps), nearest first: a step of a function is at `fn.<name>.script.<i>` wherever it is called from. At most five are listed, then `  ... and <N> more callers`. A block is not listed, since it is part of the path.

**An interrupt** (Ctrl-C, a `KeyboardInterrupt`) ends the run like any error: the breakouts run and the session is closed, as described above. An interrupt during a breakout ends that breakout; the session is still closed. Then the CLI prints `Interrupted`, with the `at` and `called from` lines of the step that was running, and exits with status 130. There is no traceback. An interrupt before the run, e.g. while the script is loaded, prints `Interrupted` alone.

**An unexpected error** is any other exception: a bug in Autobot or in a plugin, not something the script's author can fix. A plain `ValueError` or `RuntimeError` is one, and so is a `PluginError` raised while a script runs (the CLI reports a `PluginError` as a load error only from discovery). The CLI says so in one line, adds the `at` and `called from` lines when a step was running, prints the Python traceback and exits with status 70:

```
Unexpected error in Autobot: this is a bug, not a problem with the script. Please report it with the traceback below.
  at script.1 (cmd: show version)
Traceback (most recent call last):
  ...
KeyError: 'x'
```

When the step that was running is a plugin step, the first line names the plugin instead: `Unexpected error in plugin '<key>': this is a bug in the plugin, not in the script. Please report it to the plugin's author with the traceback below.` An exception outside a run, while the script is loaded or in `autobot schema`, is reported the same way, without the `at` line. `SystemExit` is not caught.

With `--traceback`, the Python traceback of a failed run, of an interrupt, and of a `Script error` or `Plugin error` is printed as well, before the report. The report and the exit status are the same as without the flag.

### Exit status

| Status | Meaning |
|--------|---------|
| 0 | The run completed; `autobot schema` printed the schema; `-h` printed the help |
| 1 | The script couldn't be loaded (the load errors above), and nothing ran. Also `autobot` with no arguments, and a `Plugin error` or a missing schema in `autobot schema` |
| 2 | A malformed command line |
| 3 | The run failed. `attach.prepare` or the session may have run, and the breakouts have run if the session got past the spawn wait |
| 70 | An unexpected error: a bug in Autobot or in a plugin |
| 130 | Interrupted |
