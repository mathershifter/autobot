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
- The mappings whose keys the script chooses: `env`, `vars` and `fn`. The models report `string_type` at the key, e.g. `vars.1.[key]`.
- The mappings with fixed keys: the top level, `attach`, a prompt, a `sendEach`, a `fields` entry, a function, a block and every step, plugin steps included. A key that isn't a string can't be one of their keys, and the models report `invalid_key`.

The mappings inside a `vars` value are data, not structure, and their keys are whatever YAML makes of them: `vars: {ports: {1: up}}` is valid, and `{{ vars.ports[1] }}` reads it. JSON has only string keys, so a JSON schema says nothing about a key's type by itself. The schema states the rule with `propertyNames: {type: string}`, on the three mappings above and on `$defs.stepCommon` (for plugin steps; the other mappings are closed, and a key that isn't a string is an additional property). That rejects the same documents as the models when the schema is applied to the document as YAML loads it, with a validator that hands it each key as it is (Python's `jsonschema` does). A validator that reads the document as JSON first, or an editor that takes `1:` for the key `"1"`, sees only strings and accepts it; there only the models reject it.

A value has the type the schema names; it is never converted from another type. A list is a YAML sequence: a `!!set`, or any other value that isn't a list, where a list goes (`script`, `errors`, a list of `cmd` lines, `expect` and so on) is a validation error (`list_type`). A string is a YAML string: a `!!binary` value isn't one, so `cmd: !!binary aGk=` or a `!!binary` `spawn` is a validation error (`string_type`) in the models as in the schema, wherever a string goes. A boolean is `true` or `false`, not `1` or `"yes"`. Numbers follow JSON, where a number with a zero fraction is that integer: `return: 1.0` is `return: 1` for both.

An optional field is either omitted or given a value of its type. An explicit `null`, including a key with an empty YAML value (`after:`, `timeout: ~`), is invalid for every optional field, including the common step properties of plugin steps: omit the key instead to get the default. The error type is `null_value`, located at the key. A plugin's own fields follow the plugin's model.

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `autobot` | yes | Schema version: exactly `2026-10`. Surrounding whitespace or a trailing newline is rejected. Any other value is a validation error (`unsupported_version`) at `autobot`: `unsupported autobot version '<value>'; expected 2026-10`, or for the earlier version `2026-08`, `autobot 2026-08 is no longer supported; use 2026-10`. That includes a value that isn't a string, e.g. what YAML makes of an unquoted `2026` (a number), `2026.10` (the number 2026.1) or `2026-10-04` (a date): the message shows the value as text, `unsupported autobot version '2026'; expected 2026-10`. Only a missing key (`missing`) and an explicit null (`string_type`) are reported as what they are. |
| `env` | no | Defaults for environment variables: string values under string keys. In a template, `env` is the whole environment: the one Autobot was started with, as `attach.prepare` changed it. So `{{ env.HOME }}` reads any variable, whether it is a key of this section or not (see [The environment in templates](#the-environment-in-templates)). A key of this section is a default: it gives the variable a value where the environment doesn't set it. A variable that the environment sets keeps its value, even an empty one, and its default isn't used. The defaults are for templates only: they aren't added to the environment of the spawned process (see [The environment of the spawned process](#the-environment-of-the-spawned-process)). Supports nesting: a default may reference other variables (e.g. `{{ env.OTHER_KEY }}`, `{{ env['OTHER_KEY'] }}`), keys of this section in any order and variables of the environment alike. Each default is rendered once, after the values it references, and once more when `attach.prepare` has run; the text it renders to isn't rendered again. Any read of a variable's value counts as a reference, including `env.get('KEY')` and `env.items()`. A variable of the environment is used exactly as it is set: its value isn't a template and is never rendered, so `{{` in it is plain text, and a default that references it gets that text. The default it overrides isn't used, so the default's references aren't followed. A reference cycle, a default that references itself directly or through other keys, is an error naming the cycle (`env cycle: A -> B -> A`), and so is a chain more than 50 keys deep. A reference to a variable that the environment doesn't set and that isn't a key of `env` is an undefined variable (`template error: env has no key 'KEY'`), so `default` applies to it. An error in a default names the default: its message starts with `env.<KEY>: `, e.g. `env.IMAGE: template error: env has no key 'BASE_URL'` or `env.HOST: template error: args has no key 'host'; pass it with --arg host=VALUE`. It names the default whose own template has the error, once, and not the defaults that reference it; a cycle and a chain that is too deep name their keys themselves and have no such prefix. In a script with `attach.prepare`, a default with such a reference waits for `prepare`, which may set the variable (see [The environment in templates](#the-environment-in-templates)). Accessible as `{{ env.KEY }}` |
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

  The `send` string is a Jinja2 template. It is rendered each time it is sent, so it sees `vars` registered by earlier steps and `session.*` as last set by an `after` or a shell prompt (the prompt match that triggers the response doesn't set them). A syntax error is reported when the prompts are loaded: before `attach.prepare` runs for the top-level `prompts`, and on entering the block for a block's `prompts`. An undefined variable is reported when the response is sent, and aborts the step waiting for the prompt. Both messages start with `prompt '<name>': `, e.g. `prompt 'confirm': template error: unexpected end of template, expected 'end of print statement'.`, and both are [template errors](#jinja2-templating).

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
| `prepare` | no | Local script to run before spawning the session (e.g. authentication, tunnel setup). A shell script is also the run's rc script: the environment variables it sets are set for the rest of the run (see [`prepare` as an rc script](#prepare-as-an-rc-script)). Rendered as a Jinja2 template first (see [Jinja2 Templating](#jinja2-templating)), with `env` as it is before the script runs. Leading spaces, tabs, line breaks, a byte order mark (U+FEFF) and the zero-width characters U+200B and U+2060 are removed from the rendered script; what remains is written to a temp file. The script is written as UTF-8, whatever the locale's encoding. If it starts with `#!`, the shebang picks the interpreter (a `\r` ending the shebang line is dropped): a shell sources the script, and any other interpreter gets it executed directly. Otherwise it runs under `/bin/sh`, and the progress line says so: `>> prepare: running local script (no shebang, using /bin/sh)`. Aborts if the script exits non-zero (`prepare script failed with exit code <N>`). If the script can't be started (e.g. the interpreter is missing or not executable), aborts with `prepare script could not run ('<first line>'): [Errno <n>] <reason>`, chained from the `OSError`. The temp file is removed when `prepare` ends, however it ends: when the script succeeds, fails or can't be started, when it can't be written to the file (e.g. a `UnicodeEncodeError` for a character UTF-8 can't encode, such as the lone surrogate a non-UTF-8 byte of an `--arg` becomes), on an interrupt, and when Autobot gets `SIGTERM` while the script runs: the script's shell is killed, the file is removed, and Autobot ends by the signal. Only a `SIGKILL` of Autobot leaves it, since no process can act on that one; the file holds the rendered script and nothing else. It is no error if the script has removed the file itself, e.g. `rm -- "$0"`: the script's exit code is what counts. Nothing is spawned after a failure. |
| `spawn` | yes | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`). Rendered as a Jinja2 template. It must not contain a NUL character (U+0000, `"\0"` in a double-quoted YAML string), anywhere: no command line can hold one. A literal NUL is a validation error in the schema and the models (`nul_character` at `attach.spawn`: `spawn must not contain a NUL character (\0): no command line can hold one`), and a `spawn` that renders to a command line with one (e.g. from a value in `vars`) is a `ValueError`, `attach.spawn rendered to a command line with a NUL character: '<template>'`, raised where an empty command is: when `spawn` is rendered (see the end of this entry). It must name a command: an empty or blank string (`''`, `'  '`) is a validation error. Blank means nothing but whitespace, and whitespace is the same fixed set of characters in the schema and the models: U+0009 to U+000D, U+001C to U+001F, the space, U+0085, U+00A0, U+1680, U+2000 to U+200A, U+2028, U+2029, U+202F, U+205F and U+3000 (so a byte order mark, U+FEFF, isn't whitespace). The error is `empty_command` at `attach.spawn`: `spawn must be a command, not an empty or blank string`. The command line is split into words as pexpect does it (whitespace separates words; `'...'`, `"..."` and `\` quote), and its first word, the command, must not be empty either: a `spawn` of nothing but quotes or a backslash (`"''"`, `'""'`, `'\'`) has no words at all, and `'' ls` has an empty first one. That is `empty_command` too, with the message ``spawn must name a command: the first word of <value> is empty (quotes or a backslash with nothing in them)`` (the value as a Python string literal), and only the models report it: a schema pattern can't split a command line (see [YAML Script Structure](#yaml-script-structure)). Whitespace before the command is not part of the command line. It is removed from the `spawn`, as written or once rendered, before the command line is checked, split and spawned, so `spawn: ' ssh host'` and `spawn: "{{ args.wrapper | default('') }} ssh host"` without a `wrapper` both run `ssh host` (pexpect by itself takes a leading space for an empty first word and finds no command). The progress line and the spawn-wait errors show the command line without that whitespace. What follows the whitespace must still name a command: `" ''"` and `" '' ls"` are `empty_command` like `"''"` and `"'' ls"`. A `spawn` with Jinja2 syntax (`{{`, `{%` or `{#`) is checked for this once it is rendered. A template that renders to an empty or blank string, or to a command line whose first word is empty, is a `ValueError`, `attach.spawn rendered to an empty command: '<template>'`, and nothing is spawned. `spawn` is rendered when the run starts, before `attach.prepare` runs, so that error, like any other template error in it, stops the run before `prepare`. The exception is a `spawn` that reads `env` (the template uses the variable `env` anywhere) in a script with `prepare`: `prepare` may set what it reads, so it is rendered once `prepare` has run, with the environment `prepare` left (`spawn: ssh {{ env.TARGET }}`, where `prepare` exports `TARGET`). Only its template syntax is checked before `prepare`; an empty command or an undefined variable stops the run after `prepare` has run, before anything is spawned. The rendered command line is printed (`>> attach: <spawn>`) and quoted in the spawn-wait errors, so a secret interpolated into `spawn` (`{{ env.TOKEN }}`) appears there: leave a secret in the environment, which the process inherits, rather than putting it on the command line. |
| `timeout` | no | Timeout for the initial spawn (duration) |
| `script` | no | Steps to run immediately after spawn (before main script) |
| `breakout` | no | Steps to run in `finally` after the main script (cleanup/disconnect) |

The attach lifecycle:
1. `attach.prepare` runs locally (if defined) — aborts on failure. With `prepare`, in this order:
   1. `attach.spawn` is rendered and checked, unless it reads `env`; then only its syntax is checked.
   2. `attach.prepare` is rendered, with `env` as it is before the script runs, and the script runs.
   3. The variables the script set and unset are applied to the environment, for templates and for the spawned process, and the `env` defaults are rendered again (see [The environment in templates](#the-environment-in-templates)).
   4. An `attach.spawn` that reads `env` is rendered and checked.

   An error in any of these stops the run before anything is spawned. Without `prepare`, `attach.spawn` is rendered and checked here.
2. `pexpect.spawn(attach.spawn)`, in [the environment of the spawned process](#the-environment-of-the-spawned-process) — waits up to `attach.timeout` (default 300s) for initial output. The output is left unconsumed, so a login or shell prompt that arrives with the banner is handled by the first prompt wait.
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

If the spawn wait (step 2) fails, the run stops there. Neither `attach.script` nor `script` runs, and **`attach.breakout` doesn't run**: the breakout undoes what the steps did on the remote (log out, leave a console server session), and no step has run or sent anything. The spawned process, if any, is closed: its pty is closed, and a process that is still running is terminated (`SIGHUP` and `SIGINT`, then `SIGKILL` if it ignores them). Closing the process is what frees the line; a silent `ssh` or `telnet` that is killed drops its connection. Then the error propagates (the CLI reports a failed run and exits with status 3, or ends from `SIGINT` after an interrupt; see [CLI](#cli)). The spawn wait fails when:
- `attach.timeout` expires before any output: `TimeoutError` (`timed out after <timeout>s waiting for the first output from '<spawn>' (attach.timeout)`, with the rendered `spawn` command). The process is still running until it is closed.
- the process exits before any output: `EOFError` (`connection closed before any output from '<spawn>'`, with the rendered `spawn` command). Once the process is closed, its exit status or the signal that ended it is appended: ` (exit status <n>)` or ` (killed by <SIGNAL>)`, e.g. ` (killed by SIGKILL)`. `SIGHUP` isn't reported, because closing the pty sends it as well; a process ended by it gets no suffix.
- the `spawn` command isn't found or isn't executable: pexpect's `ExceptionPexpect` (`The command was not found or was not executable: <command>`). No process is started.
- the operator interrupts the run (`KeyboardInterrupt`) during the wait

`attach.prepare` has already run when the spawn wait fails, and when one of the steps after it in the list above does: an `env` default that can't be rendered, or a `spawn` that reads `env` and renders to no command. Nothing undoes it; a `prepare` that sets something up (e.g. a tunnel) must clean up after itself.

Any output ends the spawn wait successfully, even if the process then exits. A process that prints a banner and exits before a prompt fails in the first step that waits on it (usually with `EOFError`), and `attach.breakout` runs as it does after any step failure. Its steps that wait on the session fail with `EOFError`; the first one ends the breakout and is logged (e.g. `>> breakout error (EOFError): connection closed while waiting for a shell prompt ('sh')`), and the original error propagates.

#### `prepare` as an rc script

A `prepare` script that a shell runs is the run's rc script: the environment variables it sets are set for the rest of the run, as if the shell that started Autobot had set them. A script sets a variable by exporting it (`export NAME=value`, or an assignment under `set -a`); an assignment that isn't exported stays in the script.

```yaml
attach:
  prepare: |
    . ~/.config/lab/credentials.sh
    export JUMP_HOST=$(lab-inventory jump-host)
  spawn: ssh -J {{ env.JUMP_HOST }} admin@{{ args.host }}
```

**Which scripts.** The script is sourced by a shell (`. <temp file>`) instead of being executed as a program when:
- it has no shebang: the shell is `/bin/sh`;
- its shebang names `sh`, `bash`, `dash`, `ksh` or `zsh`, by the interpreter's file name in whatever directory: that shell sources it. The shell is named directly, with at most the one argument a shebang line carries (`#!/bin/bash`, `#!/bin/sh -eu`), or through `env` (`#!/usr/bin/env bash`, `#!/usr/bin/env -S bash -eu`). An argument is one of two things. It is a group of the options `a`, `e`, `f`, `u`, `x` and `C` (`-eu`, `-x`): they are the script's, turned on with `set` just before the script is sourced. Or it is `-` or `--`, which ends the options and is dropped (`#!/bin/sh -`). A shebang with any other argument (`#!/bin/bash -r`, `--posix`, `-n`, `-v`, `-o <name>`) can't be sourced that way: the script is executed as a program, like one for another interpreter (see below), with the argument as written.

The script runs in the run's environment, a shell script and any other alike: the environment Autobot was started with, with `TERM=dumb` and `NO_COLOR=1` (see [The environment of the spawned process](#the-environment-of-the-spawned-process)). In the script, `$0` is the temp file and there are no positional parameters, as for a script that is executed. Because it is sourced, `return` at its top level ends it like the end of the file. With tracing on (`-x` in the shebang, or `set -x` in the script) the trace is the script's: Autobot's own commands around it aren't traced, except the two that frame it, the `.` that sources the temp file and the final `exit <N>`.

A script with any other shebang (`#!/usr/bin/env python3`, `#!/usr/bin/perl`, a shell not listed, a listed shell with another argument, `env` with other arguments) is executed as a program, and the variables it sets are not read back: a process can't change the environment of the process that started it. Everything else about it is the same: its output, its exit status, the temp file. It sets up what lives outside the environment (a tunnel, a ticket, a file).

**What is taken.** Autobot compares the environment the shell has just before it sources the script with the one it has when the script ends, and takes the differences:
- a variable that is exported with a value it didn't have before, new or changed, is set to that value;
- a variable that was in the environment before the script and isn't in it after is unset.

Only the differences are taken, not the shell's whole environment, because a shell adjusts its environment when it starts, and that isn't the script's doing. Four variables are never taken, whatever the script does with them: `_`, `SHLVL`, `PWD` and `OLDPWD`. They are the shell's own bookkeeping, and a `cd` in the script doesn't move Autobot: the working directory of the run stays what it was.

A value is taken exactly, byte for byte: line breaks, `=`, quotes, spaces, an empty value and bytes that aren't UTF-8 (see [The environment in templates](#the-environment-in-templates)) all come through. The shell's environment doesn't travel on the script's stdout or stderr, which stay the script's own (see [Output](#output)): the Python interpreter that runs Autobot, started by the shell, writes it to a second temp file. That file has no name: it is in no directory from the moment it is created, so nothing that holds the environment's values is left behind, however Autobot ends. The shell reaches it through a file descriptor it inherits, numbered 200 or above, and so do the script and every process it starts: a process the script leaves running keeps the nameless file in existence until it exits. What the helper writes is the shell's environment as the shell passed it, not what Python makes of it: Python changes its own environment when it starts (under the C locale it sets `LC_CTYPE`), so a script that exports `LANG=C` would seem to set `LC_CTYPE` too. On a system with `/proc/self/environ` (Linux) the helper reads the environment there, as the kernel recorded it when the helper started. On any other system it reads its own environment and takes `LC_CTYPE`, the one variable Python changes, as a plain `/bin/sh` started by the shell reports it. Before it writes, the helper checks that the descriptor is still that file, so a script that reuses the number for a file of its own gets nothing written into it; its environment is then not read (see below).

**When the script ends.** The script's exit status is the shell's, as for a script that is executed: the status of its last command, or of `exit <N>` anywhere in it. A script that exits non-zero aborts the run (`prepare script failed with exit code <N>`) and sets nothing, whatever it exported before it failed. A script that succeeds sets its variables however it ends: at the end of the file, with `return`, or with `exit 0`. A trap the script sets on `EXIT` runs as usual, and so does a process the script leaves running in the background: Autobot doesn't wait for it.

**When the environment can't be read.** A script that succeeds can still leave Autobot without its environment. The run goes on, no variable is set or unset, and one warning line says why: `>> prepare: environment not read: <reason>`.

| `<reason>` | When |
|---|---|
| `the script ended its shell before the shell could report it` | The script sets its own trap on `EXIT` and then calls `exit`, or replaces the shell with `exec`. Let such a script end at the end of the file, or with `return`, to have its variables taken. |
| `it could not be read after the script` | The helper that writes the environment could not be started after the script, or failed. E.g. the script exported a value too large to start any command with (more than 128 KB in one variable on Linux; the shell prints its own `Argument list too long`), or it closed or reused the descriptor of the nameless file. The shell notes this at the end of the script's temp file, which needs no command. |
| `it could not be read before the script, which ran without that` | The helper could not be started before the script, e.g. the Python interpreter that runs Autobot is not where it was. The shell sources the script all the same. |
| `Autobot doesn't know the Python interpreter it runs in (sys.executable is empty)` | There is no helper to start. The script is run without being sourced: under `/bin/sh` or as its shebang says. |

After any of these warnings, an error for a variable that isn't set says so, since a variable the script exported may be the one that is missing: `template error: env has no key 'KEY' (the environment prepare left was not read: <reason>)`, after `env.<DEFAULT>: ` when a default reads the variable.

None of these is reported as the script's failure: `prepare script failed with exit code <N>` is only ever the script's own exit status, in these cases too. They are warnings and not errors because a `prepare` may be there only for what it does outside the environment.

**What then sees the variables.** Everything that comes after `prepare`: `env` in templates has the variables as the script left them (see [The environment in templates](#the-environment-in-templates)), and the spawned process inherits them (see [The environment of the spawned process](#the-environment-of-the-spawned-process)). When the script set or unset something, Autobot prints how many variables, never their names or values: `>> prepare: environment: <N> set, <M> unset`.

#### The environment of the spawned process

The spawned process gets these variables, each line overriding the ones before it:

1. The environment Autobot was started with. So the process inherits `HOME`, `PATH`, `SSH_AUTH_SOCK`, the proxy and locale settings and everything else, as a command started from the same shell would. An entry whose name is empty (`=value`, which a program can be started with but no shell sets) is not part of the run's environment: no process can be spawned with it, and `env` in templates doesn't have it.
2. `TERM=dumb` and `NO_COLOR=1`, whatever the terminal Autobot runs in: the session is read by a program, and a plain terminal keeps colors, line editing and pagers out of its output. That holds for a process Autobot spawns locally: a device behind a console server or an `ssh` uses its own terminal type, whatever the `TERM` here. A run that gives the process another `TERM`, in `prepare` or on the `spawn` line, gets a line editor that wraps long lines and moves the cursor; the echo of a wrapped line is recognized all the same, as long as the line fits on the terminal's screen (see [The echo of a sent line](#the-echo-of-a-sent-line)).
3. What `attach.prepare` changed: the variables it set, and without the ones it unset (see [`prepare` as an rc script](#prepare-as-an-rc-script)).

Lines 1 and 2 are the run's environment from its start, and there is one environment everywhere: the `prepare` script itself runs with `TERM=dumb` and `NO_COLOR=1`, and `env` in templates has them, before `prepare` and after (see [The environment in templates](#the-environment-in-templates)). So whatever a `prepare` does to `TERM` or `NO_COLOR` is a change, and it gets its way whatever terminal Autobot was started in: `export TERM=xterm-256color` gives the process that `TERM`, and `unset NO_COLOR` gives it no `NO_COLOR`. The `TERM` and `NO_COLOR` Autobot was started with are not in the run's environment: neither a template nor `prepare` reads them.

The defaults of the top-level `env` section are not in this environment. They are values for templates; a variable reaches the spawned process only from the three lines above.

The `spawn` command is looked up in the `PATH` of this environment, so a directory that `prepare` puts in `PATH` is searched. Without a `PATH` there, it is looked up in the system default path (`/bin:/usr/bin` on Linux).

Inheriting everything has a price where the variables mean something to the command. A `spawn` that runs a local shell hands it `PROMPT_COMMAND`, `BASH_ENV`, `ENV`, `PS1` and the like, which change its prompt and what it runs when it starts, and `ssh` forwards `LANG` and the `LC_*` variables to a server that accepts them. A `prepare` can unset what is in the way.

There are two ways to give the spawned command a variable: export it in `prepare`, or set it on the `spawn` line with `env`, which is a command like any other (`spawn: env LC_ALL=C ssh host`). `spawn` is a template, so the value can come from one, e.g. from an `--arg` or a default of the `env` section: `spawn: env "SITE={{ args.site }}" ssh host`. The rendered command line is split into words, so quote a templated value as here; otherwise a value with a space in it changes which command is run. A value that may contain `"` or `\` can't be quoted this way and belongs in `prepare`. The rendered command line is also printed, so a secret interpolated there appears in the `>> attach:` line. A command that must inherit nothing is spawned the same way: `spawn: env -i PATH=/usr/bin:/bin TERM=dumb ssh host`.

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
- The terminal echo of the sent line removed (see [The echo of a sent line](#the-echo-of-a-sent-line)). Output that doesn't start with the echo is left unchanged.
- Line endings normalized to `\n`.
- ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)).
- Stray carriage returns, NULs and BELs removed where they come first in the unread output, i.e. at the start of a line or right after an escape sequence, and something other than a line break follows them (see [Prompt Handling](#prompt-handling-get_prompt)): `\rtwo\rthree` is captured as `two\rthree`. Everywhere else NULs and BELs are kept, and so are carriage returns, with one exception: carriage returns right before a line break are dropped with it, so `out\r\r` and a line break is captured as `out\n`, while `\x00` and a line break is `\x00\n`.
- The trailing partial line before the prompt match (the prompt prefix) dropped. Output that doesn't end with a newline is therefore not captured.

`errors` patterns are matched with `re.MULTILINE` against the captured output after the shell prompt returns, so they never match the echoed command, and the session is left at the prompt when the error is raised. An error that is printed without a prompt returning results in a timeout rather than an error match.

#### The echo of a sent line

A console echoes each line it is sent, and a line editor (readline, a network CLI) writes more than the line when the line reaches the right margin of its terminal: it wraps it, pads it, moves the cursor or draws part of it again. The session (`session.strip_echo`) takes the start of the output for the echo when it reads as the line that was sent. Blanks, backspaces and control characters are read as a terminal shows them; a `\r` is read more loosely than any one terminal would, since the terminal's width isn't known:

- Blanks don't count: spaces, tabs and any other whitespace are left out of both the output and the sent line before they are compared.
- A backspace moves back one character, and a character written after it replaces the one that was there. `" \b"` (a blank and a backspace, written to make the terminal wrap) shows nothing, `x\by` shows `y`, and `x\b \b` shows nothing. A backspace at the start of a line, or right after a `\r`, moves nowhere.
- The other control characters show nothing: `\x00` to `\x1f` (NUL padding, BEL, a lone ESC), DEL (`\x7f`) and `\x80` to `\x9f`. Escape sequences are already removed (see [ANSI escape sequences](#ansi-escape-sequences)).
- A line break continues the echo on the next captured line, at the point the lines before it reached. An editor that breaks the line at the margin (`\r\n`) is read this way.
- A carriage return goes back to the start of a row. Where a row starts depends on the width of the device's terminal, which Autobot doesn't know, so the text up to the next `\r` or line break is read in one of two ways: it continues the echo, or it writes again, unchanged, text of the sent line that the same captured line already shows, and may go on past it. It never goes back before the start of its captured line. `" \r"` and `"\r"` at the margin continue the echo. Readline on a terminal that wraps writes, in a single-byte locale, the character after the margin, a `\r` and that character again (`...ab\rbcd...`); in a multibyte locale it writes, after a line that ends exactly at the margin, a blank, a `\r` and the last character of the line again. Text after a `\r` that differs from what it would overwrite is not part of an echo. Each `\r` is read on its own: nothing checks that the places where the text after them is written are the row starts of one terminal width. So `reload\rload` and `abcd\rcdef` read as `reload` and `abcdef`, as a terminal shows them whose row ends after `re` or `ab`, and `sho\rhow\row` reads as `show`, which no terminal of any width shows.
- Readline's horizontal-scroll form, used with `TERM=dumb`: the first line of the output, after its last `\r`, is `<` and the end of the sent line (the part that is still visible). It counts as the whole echo.

The sent line is read the same way, so a backspace or another control character in it counts for what a terminal shows of it. A device that echoes such a character as text (`^G`) doesn't echo the line in this sense.

The echo ends at the first line break by which all of the sent line is shown, and the output starts after that line break. Lines before the echo that show nothing go with it: empty lines, and lines of only blanks and control characters (`\x07`, a line break, `\x00`, a line break, `ls`, a line break and `out` is captured as `out` for the sent line `ls`). Only the echo is read this way: the output after it is kept as described above, control characters included, and a second line that reads as the sent line is output.

The output is left unchanged, echo included, when it doesn't start with the echo: the echo is disabled (`stty -echo`, a password), the device writes something else for the line, text follows the echo on its last line, or the sent line shows nothing (an empty line, only blanks or control characters). Nothing is reported for it. One case is beyond the comparison: with the echo disabled, output whose first line reads as the sent line is taken for the echo.

Forms that are not recognized, for which the echo stays in the captured output:

- A long line shown scrolled sideways with a marker other than readline's `<`, e.g. the `$` some network CLIs put at the edge of a line that doesn't fit.
- A control character of the sent line echoed as text (`^C`, `^G`).
- Escape sequences that the session doesn't remove (see [ANSI escape sequences](#ansi-escape-sequences)), e.g. `ESC 7` and `ESC 8`, and text that an editor writes at a place it moved to with a removed sequence, when that text differs from what the echo shows there.
- Anything else the editor writes into the line, such as a completion or the prompt of a continuation line.
- A line that doesn't fit on the screen of a terminal that wraps. The terminal of a process Autobot spawns has 24 rows of 80 columns, so that is a line of 1920 characters with its prompt (960 after `stty cols 40`). Readline then clears the screen and writes the prompt and the line: the prompt comes first, with nothing of the echo before it, so it is not held (see [A prompt inside the echo](#a-prompt-inside-the-echo)), the prompt wait ends at it, and the command's output is not captured. With `TERM=dumb` a line of any length is echoed in the `<` form and is recognized.

The reading is bounded, so that output which is no echo is left alone quickly whatever its size. A captured line is no part of an echo when one of its parts between two `\r` is longer than 8 characters per character of the sent line plus 1024, blanks and control characters included, or when it has more parts that show something than twice the characters the sent line shows, plus 2. And where a sent line repeats itself (`=====`), so that text after a `\r` fits in many places, 8 of the readings are kept: the one that continues the echo, and those that reach furthest. An echo past these bounds is not recognized, and the output is left unchanged; a prompt written again after such a part is not held either (see [A prompt inside the echo](#a-prompt-inside-the-echo)).

A line editor may write the prompt again while it echoes the line: readline does it in a single-byte locale (`LC_ALL=C`), on a terminal that wraps, for a line that with its prompt ends exactly at the right margin (a blank, a `\r`, cursor-up, the prompt and the line again). That prompt is not the end of the command, and the prompt wait holds it instead of ending at it (see [A prompt inside the echo](#a-prompt-inside-the-echo), which also says where this stops working); in the captured output it counts as a `\r`, so what follows it is read as the row written again.

#### ANSI escape sequences

The session removes ANSI escape sequences that match `types.ANSI_ESCAPE_RE`: CSI sequences (`ESC [`, parameter and intermediate bytes, a final byte; e.g. colors, `ESC [?2004h`, cursor movement) and two-byte `ESC` sequences whose second byte is `@` through `Z` or `\` through `_`. Other sequences are left in the text, e.g. `ESC ( B`, `ESC =`, `ESC 7`, the 8-bit CSI byte `0x9B`, and the body of an OSC sequence such as a terminal title (only its `ESC ]` is removed). The spawned process's environment has `TERM=dumb` and `NO_COLOR=1` unless `attach.prepare` changed them (see [The environment of the spawned process](#the-environment-of-the-spawned-process)), so many programs print no color to begin with.

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

Other values are used verbatim, e.g. `expect`, `errors`, `control`, `call` and `register`. A plugin step decides which of its own fields it renders.

Any template error in any templated value, a syntax error or an undefined variable, is reported as a `ValueError` (a `ScriptError`, see [CLI](#cli)) with the message `template error: ...`. So is any other exception an expression raises while the value is rendered: the message then names the exception's type, `template error: <type>: <message>` (just `<type>` when the exception has no message), e.g. `template error: ZeroDivisionError: division by zero` for `{{ 1/0 }}`, or `template error: TypeError: can only concatenate str (not "int") to str` for `{{ 'a' + 1 }}`. The `ValueError` is chained from the original exception. An error that is already reported is not wrapped again: a template error from a render inside the render (an `env` default referenced by another, a failing `search` regex) keeps its message, and an `env` cycle or nesting error stays `env cycle: ...` (see [Top-level fields](#top-level-fields)). `KeyboardInterrupt` and `SystemExit` are not wrapped. This includes plugin fields rendered through the runner. It always aborts the step, and with it the script, even with `ignore_error: true`. In a breakout it ends that breakout and is logged, like any other breakout error.

Because these values are templates, `{{`, `{%` and `{#` in them always start Jinja2 syntax, even inside shell code. For example, bash's array length `${#arr[@]}` contains `{#`, which opens a Jinja2 comment, so rendering fails with a template syntax error. To pass such text through literally, wrap it in `{% raw %}...{% endraw %}`, or emit the delimiter from an expression (`{{ '{#' }}`):

```yaml
attach:
  prepare: |
    #!/bin/bash
    arr=(a b c)
    {% raw %}echo "${#arr[@]}"{% endraw %}
  spawn: ssh host
```

Available context:

| Variable | Source |
|----------|--------|
| `env` | The run's environment: every variable Autobot was started with, `TERM=dumb` and `NO_COLOR=1`, as `attach.prepare` changed them, and the defaults of the YAML's `env` section for the variables that aren't set (see [The environment in templates](#the-environment-in-templates)) |
| `vars` | `vars` section of the YAML (also populated at runtime by `cmd` steps with `register`) |
| `args` | CLI `--arg KEY=VALUE` arguments: a mapping of the keys given, each value a string (see below for a key that wasn't given) |
| `session.before` | Text captured before the last `after` match (pexpect `before`), or the captured output of the last command when a shell prompt is reached (empty if it printed nothing). The `$?` check, embedded-script cleanup and the first prompt wait of a `cmd` with `after` don't change it. |
| `session.match` | Text that matched the last `after` pattern (pexpect `after`), or the text the prompt regex matched when a shell prompt is reached. After an `after` match, `session.before` and `session.match` are raw text; after a shell prompt they have ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)). |

`env`, `vars`, `args` and `session` are mappings. On a mapping, `x.name` and `x['name']` both read the key `name`, and with `x.name` a key always wins over a mapping method of the same name: with `register: values`, `{{ vars.values }}` is the registered output, not the `values` method (plain Jinja2 would render `<built-in method values of dict object ...>`). The same goes for `items`, `keys`, `get`, `copy` and the rest, and for mappings nested in `vars` (`vars.site.values`). A method is reachable as `x.name` only while there is no key of that name, so `vars.items()`, `vars.get('k', 'default')` and `env.get('KEY')` work as long as no key is named `items` or `get`. Filters don't depend on key names: `vars | items`, `vars | length`, `vars | tojson`.

An argument that wasn't given is not in `args`. `{{ args.KEY }}` and `{{ args['KEY'] }}` are then an undefined variable, a template error that says how to give it: `template error: args has no key 'KEY'; pass it with --arg KEY=VALUE`. It is raised where the template is rendered: when the script is loaded for an `env` default (`env.HOST: template error: args has no key 'host'; ...`), during the run for a step. `{{ args.KEY | default('x') }}`, `args.get('KEY', 'x')`, `'KEY' in args` and `args.KEY is defined` deal with it as they do on any mapping.

Built-in global: `range`. Use Jinja2 filters for other operations (e.g. `{{ items | length }}`).

### The environment in templates

`env` holds one value for each variable name, taken from the first of these that has it:

1. the run's environment: the one Autobot was started with, with `TERM=dumb` and `NO_COLOR=1` set over it, then with the variables that [`attach.prepare`](#prepare-as-an-rc-script) set and without those it unset,
2. the default of that name in the YAML's `env` section (see [Top-level fields](#top-level-fields)).

So the precedence is, lowest to highest: a default of the `env` section, the environment Autobot was started with, Autobot's `TERM=dumb` and `NO_COLOR=1`, a variable set by `prepare`. A variable that `prepare` unset is not in the environment: its default applies, if it has one. This is the environment the spawned process gets (see [The environment of the spawned process](#the-environment-of-the-spawned-process)): `{{ env.TERM }}` is `dumb` unless `prepare` changed it, whatever terminal Autobot runs in.

So `{{ env.HOME }}`, `{{ env['SSH_AUTH_SOCK'] }}` and `{{ env.get('HTTPS_PROXY', '') }}` work without an `env` section, and a key of the `env` section is needed only to give a variable a default. A variable that is set to an empty string is set: `env.X` is `''`, and its default isn't used. The environment is read once, when the run starts, and changes once, when `prepare` has run.

So `env` has two states, and a template sees the one of the moment it is rendered:
- Before `prepare` has run: the environment Autobot was started with, with `TERM=dumb` and `NO_COLOR=1`, and the defaults rendered against it. `attach.prepare` itself is rendered in this state, and so is an `attach.spawn` that doesn't read `env`.
- Once `prepare` has run: the environment as the script left it, and the defaults rendered again, against it. Everything else is rendered in this state: an `attach.spawn` that reads `env`, every step, every prompt's `send`. A script without `prepare`, or whose `prepare` changes nothing, has the same `env` in both.

The defaults are rendered once for each state. So after `prepare`, a default that references a variable `prepare` set has the new value, and a default whose own variable `prepare` set is not used: `env.KEY` is the value `prepare` set, also when the `prepare` template itself read the default.

In a script with `prepare`, a default may reference a variable that only `prepare` sets. When the script is loaded, a default that reads a variable set nowhere (directly or through another default) is therefore not an error: it waits. Until `prepare` has run it has no value: reading it in the `prepare` template is an undefined variable, `template error: env.KEY can't be read before prepare has run: env has no key 'OTHER'`. After `prepare` it is rendered like the others, and if its variable is still set nowhere, the run fails there, before anything is spawned (`env.KEY: template error: env has no key 'OTHER'`, a failed run; see [CLI](#cli)). Only this one error waits. Every other error in a default (a syntax error, any other template error, a reference cycle, a chain that is too deep) is reported when the script is loaded, with or without `prepare`, and against the environment of that moment: a cycle among defaults is an error even if `prepare` would set one of its keys. In a script without `prepare`, a default that reads a variable set nowhere is a load error like the others.

A variable that neither has is not in `env`. `{{ env.KEY }}` and `{{ env['KEY'] }}` are then an undefined variable, a template error with the message `template error: env has no key 'KEY'`; `{{ env.KEY | default('x') }}`, `env.get('KEY', 'x')`, `'KEY' in env` and `env.KEY is defined` deal with it as they do on any mapping.

`env` is one mapping of all these variables, and its methods and the filters see all of them: `env | length` counts them, `env | items`, `env.keys()` and `{% for name in env %}` go through them, and `{{ env | tojson }}` writes them all out, values included. The keys of the `env` section come first, in the order written, whether the environment sets them or not; the other variables follow, sorted by name. Every value is a string. The mapping holds the variables and nothing else: a name that isn't a variable or a mapping method, such as `env.later`, is undefined like any unset variable. A value of the environment that isn't valid UTF-8 keeps its other bytes as lone surrogates, as Python's `os.environ` reads it. The spawned process gets such a value in its environment byte for byte. A template gets it as text: sent to the session (in a `cmd`, a `line` or a prompt's `send`), each of those bytes goes out as `?`, since the session's encoding is UTF-8 with replacement; rendered into `attach.prepare`, it can't be written to the script's temp file, which is strict UTF-8, and the run fails with a `UnicodeError` (see [CLI](#cli)). The echo of such a `cmd` line differs from the text that was rendered, so it isn't removed from the command's captured output (see [Captured output](#captured-output)).

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
- A value of `env` is a string. A boolean there (`DEBUG: true`, `NO_COLOR: yes`) is `string_type` too, and the models say what to do: `an environment value is a string, and unquoted this one is a boolean (true); quote it to set it as written, e.g. 'true' or 'yes'`.
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

The session is *at a shell prompt* from the moment a prompt wait ends at one, or an `after` wait reads text that ends with one (the prompt's match ends where the text read ends, nothing more has been read, and the prompt is not one a prompt wait would hold, see [A prompt inside the echo](#a-prompt-inside-the-echo)), until anything is sent (a command line, the `$?` check, `line`, `return`, `control`) or the session is closed. A closed session is at no prompt: a prompt wait on it fails with `RuntimeError` (`not attached`), like every other use of it. Nothing of a closed session applies to a process spawned after it: the first wait on the new process follows no command, so it solicits, the echo of a line sent to the old one isn't looked for, and `session.before` and `session.match` start empty. A prompt wait while the session is at a shell prompt returns at once, without reading output, sending anything, or changing `session.before` and `session.match`. A block's prompt swap and restore can end this state but never start it (see [Prompt state across a swap](#prompt-state-across-a-swap)).

While it waits, the engine also consumes each `\r\n` and each ANSI escape sequence as it arrives: the text before it goes to the captured output, and the escape sequence itself is dropped (see [ANSI escape sequences](#ansi-escape-sequences)). It also discards stray control characters at the very start of the unread output: a run of carriage returns (`\r`), NULs (`\x00`) and BELs (`\x07`) that is followed by any character other than these and a line break. Some devices and console servers print them before a prompt (a `\r` to return the cursor, NUL padding, a bell). The run is discarded only once the character after it has arrived, so a `\r` whose `\n` comes in a later read is still the line break `\r\n`, and a run that is followed by a line break is not discarded as stray: it is part of that line. Its NULs and BELs are captured (`\x00\r\n` is `\x00\n`), and its carriage returns go with the line break, like any carriage returns that end a line (`\r\r\n` is an empty line, `out\r\r\n` is `out\n`). Only the start of the unread output is concerned, that is, what follows a line break, an escape sequence, an answered prompt or whatever the previous wait consumed: these characters anywhere else, such as the `\r` of a line that rewrites itself (`10%\r50%\r100%`), are captured as they are, except for carriage returns right before a line break. When several patterns match, the one that starts earliest in the unread output wins; on a tie, `\r\n` comes first, then an escape sequence, then the prompts in the order they are defined, and the stray characters last (so a prompt regex that itself starts with `\r` still matches there).

A bare `\n` without `\r` is not a line break for the engine and is not consumed. A pty turns every `\n` a program writes into `\r\n`, so this only concerns a remote that has turned that off or a raw line that sends `\n` alone: there a prompt that follows output is not at the start of the unread output, a regex anchored with `^` doesn't match it, and the prompt regex must be left unanchored.

Prompt regexes (`expect`, and the `match` regexes of `sendEach` `fields` entries) are compiled with `re.DOTALL` and searched for anywhere in the unread output; they aren't anchored at its start. There is no `re.MULTILINE`, so `.` matches line breaks and `$` matches only at the end of the unread output (or before a final `\n`). A shell prompt regex should end at the prompt character (e.g. `'^\w+@[\w.-]+:[^\r\n]*[$#] ?$'`, the regex of `examples/sonic.autobot.yaml` for `admin@sonic:~$ `): any part of the prompt it leaves unmatched stays in the stream and becomes the start of the next command's captured output, where the echo of the command is then no longer recognized and removed. It should also describe the whole prompt, not just its last character: because the search isn't anchored, a regex such as `[>#$] ?$` matches any output that happens to end a read with `>`, `#` or `$`, and the command that follows is sent into whatever is still running. A regex that describes the whole prompt can still be found inside output: without the `^`, the one above matches `scp admin@host:/x $` when that ends a read. So start the regex with `^`, as both examples do: it anchors the prompt to the start of its line. `^` matches at the start of the unread output, and that is the start of a line whenever a prompt is due, because the engine has consumed the line breaks and escape sequences before it and discarded any stray `\r`, NUL or BEL in front of it. An anchored regex doesn't match a prompt that has other text before it on its line: one printed right after output with no final newline (`printf foo`), or one that follows a `\r` in the middle of a line (text, `\r`, then the prompt). Leave the regex unanchored for a device that does that.

The prompt engine polls the session output in 5-second intervals:
1. If a prompt with no `send` (or `return: true`) matches → return (shell prompt reached), unless the prompt is held as one that may be inside the echo of the line that was sent (see [A prompt inside the echo](#a-prompt-inside-the-echo))
2. If a prompt with `send` values matches → send the response chosen as described in [Response selection](#response-selection) and continue waiting
3. On 5-second timeout with no match → send a single empty newline to solicit a prompt (once only, only if no handler has been activated yet, never when the wait follows a command, and never while a prompt is held; see below)
4. On overall timeout → raise `TimeoutError` (`timed out after <timeout>s waiting for a shell prompt ('<name>', ...)`, naming the current prompts that are shell prompts, or `none defined`)
5. If the connection closes (the process exits) → raise `EOFError` at once (`connection closed while waiting for a shell prompt ('<name>', ...)`, with the same names)

This handles idle consoles that need a return press to display a prompt.

#### A prompt inside the echo

A line editor may write its prompt again while it echoes the line it was sent (readline does, see [The echo of a sent line](#the-echo-of-a-sent-line)). Such a prompt is not the end of the command: the rest of the echo, its line break and the command's output are still to come. So a shell prompt is *held*, and doesn't end the wait, when both hold for what the wait has read up to the prompt:

- it has no line break, and
- read as a terminal shows it, it is the start of the line that was sent, at least one character of it, or the whole line.

A held prompt counts as a `\r` in the captured output, so the line written after it reads as the row written again and the echo is removed.

What the wait has read is what this prompt wait itself read. Text that an `after` wait or a plugin's `session.expect` consumed before it is not counted, with one exception, below. The line that was sent is the last one sent before the wait with a `cmd` line, a `line`, `return`, the `$?` check or a plugin's `session.sendline`; the solicit newline a wait sends on its own doesn't change it, so a wait that solicits goes on comparing with the line sent before it. A prompt is never held after a line that shows nothing: an empty `cmd`, a `return`.

A prompt is not held when a line break came before it (the usual case: an echo ends with one, whether or not the command prints anything), or when nothing was read before it or what was read is not the start of the sent line (the echo is off, as for a password; other output; `^C`).

**When a held prompt ends the wait after all.** A device may echo the line and print its prompt after it on the same line, with no line break, and a command's output may happen to be the start of the command (`printf pr` with the echo off). That prompt looks the same as one written again, and it is held. The wait then polls in periods of `session.HELD_GRACE`, 1 second (or what is left of the wait's timeout, if less), and takes the held prompt for the prompt at the end of the first period in which nothing at all arrived: no byte, matched or not. It ends there as it would have without the hold, one period later, and two when something unmatched arrived during the first (the blank after the prompt, say, in a write of its own). Whatever the prompt regex left unmatched stays unread, as it does after any prompt. No solicit newline is sent while a prompt is held.

Readline writes the prompt and the rest of the line in one write, so the period is not for readline; it is for a slow line between a device and what Autobot talks to. The bound of the rule is that period: if a device writes its prompt again inside the echo and then sends nothing for a whole period before the rest of the echo, the prompt is taken for the prompt, the next command is sent early and the captures that follow are off by one command. An escape sequence that arrives after the held prompt starts a new period but is not the rest of the echo, so the same holds for a device that writes the prompt, a sequence such as `ESC[K`, and then pauses that long. The hold also depends on the echo being read as one: a part of it past the bounds of that reading (see [The echo of a sent line](#the-echo-of-a-sent-line)), such as more NUL padding in one part than 8 characters per character of the sent line plus 1024, is not read as the start of the sent line, and the prompt after it ends the wait.

**After an `after` wait.** An `after` wait that reads text ending with a prompt that would be held, with nothing read after it and no line break in what the `after` wait read, doesn't put the session at a shell prompt. It hands the prompt on, with what it read before it, to the next prompt wait, which holds it as if it had read it itself: it goes on if more of the echo comes, and takes it for the prompt after a quiet period otherwise. Anything sent in between (`line`, `return`, `control`) drops it.

**A command that contains the prompt.** With a prompt regex that isn't anchored, a command whose text contains the prompt (`echo 'PROMPT$ '` for the regex `PROMPT\$ `) has that prompt found inside its own echo. It is held, and the wait goes on to the real prompt, but the echo is not recognized, since the prompt's text is missing from it: the captured output starts with the mangled echo (`echo '\r'`). A regex anchored with `^` doesn't find a prompt there.

The solicit newline is for a console that is idle, not for a command that is still running. A wait that follows a command never solicits, however long the command stays silent: the wait after a `cmd` line, after the `$?` check, after each line of an embedded-script upload and its cleanup `rm`, and after a plugin's `session.sendline(...)`. A shell would answer the newline with a second prompt once the command ends, and that stale prompt would end the next wait early, so each later step would capture the output of the command before it. A wait solicits when the last thing sent before it wasn't a command: nothing at all (the first wait after the spawn, a wait after a prompt swap, or the wait after one that timed out), or a raw send, which doesn't wait for a prompt itself (`line`, `return`, `control`, the `^C` of an embedded-script cleanup). The one exception is the first prompt wait of a `cmd` with `after`: it follows output, not silence, and never solicits (see [`cmd`](#cmd--send-commands-to-the-shell)). So a `cmd` after `line: consutil connect 0` still gets the return press an idle console needs, while `cmd: consutil connect 0` itself would wait for a prompt that never comes: start anything that shows nothing until Return is pressed with `line`. A `$?` check that fails before its result arrives (a timeout or a closed connection) counts like a prompt wait that timed out: the next wait solicits. A plugin's `session.sendline(text)` is a command; a plugin marks a raw send, one whose prompt it doesn't wait for, with `session.sendline(text, solicit=True)`.

## CLI

```
autobot [run] <script.yaml> [-a KEY=VALUE ...] [--traceback]
autobot validate <script.yaml> [<script.yaml> ...] [-a KEY=VALUE ...] [-q] [--traceback]
autobot schema [--traceback]
autobot -h | --help
```

Subcommands:
- `run <script>`: load, validate and execute the script. `script` is the path to the YAML script file.
- `validate <script>...`: load and validate each script as `run` does, and run nothing (see [Checking scripts with `validate`](#checking-scripts-with-validate)).
- `schema`: print the JSON schema to stdout (see below). It takes no arguments, and one option, `--traceback`.

`run` is the default. If the first argument isn't `run`, `validate`, `schema`, `-h` or `--help`, the CLI treats the command line as `autobot run ...`, so `autobot <script>` is the same as `autobot run <script>`, and options may come before the script (`autobot -a k=v <script>`). A script file named `run`, `validate` or `schema` must be given with the subcommand (`autobot run schema`) or as a path (`autobot ./schema`).

Options of `run`:

| Flag | Description |
|------|-------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | Pass an argument to the script, accessible as `{{ args.KEY }}`. Repeatable, one `KEY=VALUE` per flag. The value is everything after the first `=`, so it may contain `=`. Values are strings. If a key is given more than once, the last value wins. |
| `--traceback` | Also print the Python traceback of an error that is reported without one (see [Errors while the script runs](#errors-while-the-script-runs)). |
| `-h`, `--help` | Print the `run` usage and exit with status 0. |

`validate` takes the same three options, and `-q` (see [Checking scripts with `validate`](#checking-scripts-with-validate)).

`autobot -h` prints the list of subcommands and exits with status 0. `autobot` with no arguments prints the same help to stdout and exits with status 1.

`autobot schema` prints the `2026-10` JSON schema, indented, to stdout, extended with the step types of installed plugins. It first loads the plugins registered in the `autobot.steps` entry-point group, as `run` does, so a plugin that can't be loaded is reported (`Plugin error: ...`) before anything is printed. Then it reads the schema that ships with the package: `autobot/autobot.2026-10.json`, a copy of `schemas/autobot.2026-10.json` that every wheel and source distribution contains. The command never uses the network, and an installed autobot prints the schema of its own version. The repository keeps one copy of the schema, in `schemas/`; the file in the package directory is a link to it, which a build replaces with the file. In a source tree where the file in the package directory is missing or isn't the schema, the command reads `schemas/autobot.2026-10.json` directly; a checkout made without symbolic links has a small text file there that holds the link's target. If the schema is in neither place, the CLI prints `Cannot read the schema: neither <packaged file> nor <schemas file> holds the JSON schema` on stderr and exits with status 1, without a traceback. A wheel must be built from a tree where the link is a link: built from a checkout without symbolic links it would ship that text file, and its `autobot schema` would fail this way. For each plugin it adds `$defs.<key>Step`, and a `$ref` to it in `$defs.step.oneOf` just before the final `pluginStep` entry. `<key>Step` requires the plugin's key and combines the [common step properties](#common-step-properties), from the static schema's `$defs.stepCommon`, with the plugin model's pydantic JSON schema, which describes the plugin's own fields. Any other key is rejected, unless the plugin's model allows extra fields (the model's `additionalProperties` becomes the definition's `unevaluatedProperties`). Definitions nested in the plugin's schema stay under `$defs.<key>Step.$defs`. The definition is named with the key as it is. In the `$ref`s that point to it or into it, the name is escaped as a JSON pointer in a URI fragment requires (`~` as `~0`, `/` as `~1`, and other characters outside letters, digits and `-._~` percent-encoded), so a key such as `a/b` or `a b` gets a working definition (`#/$defs/a~1bStep`, `#/$defs/a%20bStep`). When the model refers to itself, its pydantic schema is a `$ref` to its own definition; a copy of that definition takes the `$ref`'s place in `<key>Step`, so the step accepts the common step properties, and the definition stays under `$defs.<key>Step.$defs` for the nested references, which accept only the model's own fields. The plugin's key is also added to the keys `pluginStep` excludes, so a step with that key matches only `<key>Step`: it is checked against the plugin's model and the common step properties, and an invalid one is rejected rather than accepted as an unknown plugin step. A step with a key no installed plugin registers still matches `pluginStep`, which applies `stepCommon` in both the static and the generated schema: its common step properties are checked, and its other keys are not.

A run that completes prints `>> run completed` as its last line on stderr and exits with status 0.

The script file is read as a single YAML document, encoded as UTF-8 (or UTF-16 with a byte order mark). Keys must be unique in every mapping at every level (top level, `attach`, `env`, `vars`, `fn`, prompts, steps, and any nested value). A repeated key is an error rather than overriding the earlier value. Keys are compared as loaded, so `x` and `"x"` are the same key, while `1` (an integer) and `"1"` (a string) are different keys. A key set next to a `<<` merge key overrides the merged value and isn't a duplicate. A mapping can have only one `<<` key; to merge several mappings, use `<<: [*a, *b]`. The CLI reports these load errors on stderr without a traceback:

| Error | First line |
|-------|------------|
| An installed plugin can't be loaded: its entry point fails to import or to create the executor, or the plugin is rejected at registration (an executor without a usable `key`, `model` or `execute`, a reserved key or common-name field, a key another plugin registered, or the model of a built-in step; see [Common Step Properties](#common-step-properties)). Also reported by `autobot schema` | `Plugin error: <message>`, e.g. `Plugin error: entry point 'echo' (distribution pkg-b) failed to load: ModuleNotFoundError: No module named 'foo'` or `Plugin error: plugin pkg_b.EchoExecutor (distribution pkg-b, entry point 'echo'): step key 'echo' is already registered by plugin pkg_a.EchoExecutor (distribution pkg-a, entry point 'echo')` |
| The file can't be read (missing, a directory, permission denied) | `Cannot read script <path>: <reason>` |
| The file isn't valid YAML (syntax error, tab indentation, more than one document, an undefined alias, an unsupported tag such as `!!python/object`, a duplicate key) | `YAML error in <path>, line <L>, column <C>: <problem>`, followed by an indented context line when YAML gives one. For a duplicate key the problem is `found duplicate key '<key>'` at the repeated key, and the context line is `  first defined (line <L>, column <C>)` |
| A plain value or key reads as one of YAML's types and isn't a value of it: a date or time that doesn't exist (`2001-99-99`, a time zone of `+99:99`), an integer of more digits than Python reads (4300), `0x_` | `YAML error in <path>, line <L>, column <C>: invalid <type> (<reason>); quote the value if it is meant as text`, at the value, e.g. `invalid timestamp (month must be in 1..12); ...` or `invalid int (Exceeds the limit (4300 digits) for integer string conversion: ...); ...`. `<type>` is the short name of the YAML tag, and `<reason>` is Python's |
| The document is nested deeper than the parser can follow (hundreds of levels, e.g. 3000 `[`) | `YAML error in <path>: the document is nested too deeply` |
| The file has bytes that aren't valid UTF-8, or disallowed control characters | `YAML error in <path>, position <N>: <reason> (...)` |
| The script fails validation, including an empty file, a document that isn't a mapping, an undefined `call` target or invalid plugin step fields | `Validation errors:`, followed by one line for each error (see below) |
| An `--arg` has no `=` | `--arg requires KEY=VALUE format, got: <arg>` |
| stdout is closed (`autobot run script.yaml >&-`), so the session's output has nowhere to go. Use `> /dev/null` to discard it. Only `run` reports it | `Cannot write the session's output: stdout is closed (redirect it to /dev/null to discard the output)` |
| The top-level `env` can't be resolved (a template error, a reference cycle, or nesting more than 50 keys deep; in a script with `attach.prepare`, a default that reads a variable set nowhere waits for `prepare` instead, see [The environment in templates](#the-environment-in-templates)), a top-level prompt `send` string has a template syntax error, or a top-level prompt's `sendEach` collection can't be resolved (see [`sendEach`](#sendeach)) | `Script error in <path>: <message>`, e.g. `Script error in <path>: env.IMAGE: template error: ...`, `Script error in <path>: env.HOST: template error: args has no key 'host'; pass it with --arg host=VALUE`, `Script error in <path>: env cycle: A -> B -> A`, `Script error in <path>: prompt 'confirm': template error: unexpected end of template, expected 'end of print statement'.` or `Script error in <path>: prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'` |

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
- The location is the error's location as the models report it, its keys and indexes joined with `.` (a step's type tag included, as in `script.0.cmd.timout`). One case is reported differently from the models' own list of errors: a field that takes a string or a list of strings (`cmd`, `line`, `control`, `assert`) and gets neither. The models have two errors for it, at `<field>.str` and `<field>.list[str]`; the report has one, at the field, with the message `Input should be a string or a list of strings` and the type `string_type`. For a list with an item that isn't a string, the report has the item's error alone, at `<field>.<index>`. An entry of a prompt's `expect` that isn't a string is one error too, `Input should be a valid string` at `prompts.N.expect.M`. An error of the document as a whole, such as a file that is empty or holds a list, is at `(document)`.
- The message is the error's message; pydantic's `Value error, ` prefix is left out.
- The value is the offending value as YAML writes it: a string in quotes, a number, `true`, `false` or `null`. A mapping is shown as `a mapping with the keys <key>, ...` (`an empty mapping`), a list as `a list of <N> items`, and any other value by its YAML type (`a timestamp`, `binary data`). What is shown is cut after 60 characters with `...`. Whether a value is shown depends on the error's type, not on the wording of the message. None is shown for `missing` and `extra_forbidden`, where the location says it all; for `string_too_short` and `too_short`, where the value is empty; and for the types whose message is written with the value in it: `value_error` (e.g. `invalid duration: 5 minutes`), `assertion_error`, `unsupported_version`, `control_char`, `invalid_regex` and `undefined_function`. For every other type the value is shown, unless the message quotes that very string in full, as the second `empty_command` message does. No value is ever shown for an error at a prompt's `send` or anything inside it (the parts of a `sendEach` included, in top-level and block `prompts`), at a `line` step or one of its lines, or at `env` or one of its values: what goes there is sent to the device or is the value of a variable, and may be a password. The messages for those locations don't show the value either, except that the message for a boolean `env` value says which boolean it is (`true` or `false`).
- The type is the error's type, as named throughout this document (`invalid_regex`, `string_type`, ...).

Each error is one line, whatever its message, location or value holds: the report is never wrapped, and a character in them that isn't printable (a line break, a tab, ESC or another control character, which a message may show as part of a value of the script) is written as its escape, e.g. `\n`, `\t`, `\x1b`. So a value can't break a line of the report or send an escape sequence to the terminal. That holds for the messages of a plugin's model as well. On a terminal the location is bold and the type dim (see [Output](#output)).

**The script's path in a report.** Wherever a report names the script (`Cannot read script <path>`, `YAML error in <path>`, `Script error in <path>`, `Run failed in <path>`, the `<path>: valid` and `<path>: invalid` lines and the `while checking <path>` line of `autobot validate`), `<path>` is the script as given on the command line, with each character that isn't printable written as its escape, as in a validation report: a line break as `\n`, a carriage return as `\r`, a tab as `\t`, ESC as `\x1b`, and a byte of the name that isn't UTF-8 as the escape of the lone surrogate it is read as (`\udcff`). A file name may hold any of these, and so the path is always on one line: it can't end a line of the report and start one that reads like another script's, and it can't send an escape sequence to the terminal.

Line and column numbers start at 1; `position` is a 0-based offset into the file. With `--traceback`, a `Plugin error` and a `Script error` are preceded by the Python traceback of the exception they report. For each of these errors the CLI exits with status 1, and nothing runs: `attach.prepare` isn't run and no session is spawned. Only the first error is reported (`autobot validate` reports more where it can, see [Checking scripts with `validate`](#checking-scripts-with-validate)). A closed stdout is checked before anything else. The installed plugins are loaded next, because validation depends on them, so a broken plugin is reported even when the script itself has an error. Then the file is read and parsed, then validated, then `--arg` values are checked, then `env` and `prompts` (after `--arg`, because `env` may use `{{ args.KEY }}`). Command-line syntax errors caught by the argument parser, such as `--arg` with no value, `run` without a script, an unknown option, or an argument to `schema`, print usage and exit with status 2.

### Checking scripts with `validate`

`autobot validate <script>...` says whether each script would load, without running any of them. It is the command for an editor hook or a CI job. It loads a script with the code `run` loads it with, so the two can't disagree, and stops where `run` would start to run it:

- **Nothing runs.** `attach.prepare` isn't run, no process is spawned, nothing is sent anywhere and no file is created, a temp file included. Nothing is written to stdout, so a closed stdout is not an error here.
- **Plugins are loaded**, because validation depends on them. That imports each installed plugin's module and creates its executor, so whatever a plugin does when it is imported happens under `validate` too. They are loaded once, before the first script is read. A plugin that can't be loaded is reported as by `run` (`Plugin error: ...`); the command then exits with status 1 and checks no script.

Each script is then checked in the order given, with the sequence of `run`: the file is read and parsed, then validated, then the `--arg` values are checked, then `env` and `prompts`. Everything is written to stderr, as plain or styled text by the rules of [Output](#output):

- A script that loads gets one line, `<path>: valid`.
- A script that doesn't gets the report `run` prints for the same error, character for character (`Cannot read script ...`, `YAML error in ...`, `Validation errors:` and its lines, `--arg requires ...`, `Script error in ...`), and then the line `<path>: invalid`. The report masks what a validation report masks.

`<path>` is the script as given on the command line, with each character that isn't printable written as its escape (see below). The `invalid` line is what ties a report to its file, since `Validation errors:` and `--arg requires ...` don't name one. The scripts are given together, with the options before or after them: an option between two scripts is a malformed command line. A script given twice is checked twice.

```
$ autobot validate upgrade.autobot.yaml login.autobot.yaml lab.autobot.yaml
upgrade.autobot.yaml: valid
Validation errors:
  script.0.cmd.timout: Extra inputs are not permitted [extra_forbidden]
login.autobot.yaml: invalid
Script error in lab.autobot.yaml: env cycle: A -> B -> A
Script error in lab.autobot.yaml: prompt 'login': sendEach 'vars.creds': item 1 has no field 'password'
lab.autobot.yaml: invalid
```

**Which errors a script gets.** The sequence stops at the first stage that fails, as in `run`, because each stage needs what the one before it gives: an unreadable file has nothing to parse, and a script that fails validation has no `env` to resolve. Within the last stage `validate` goes on where `run` stops: it reports the error of `env`, and then the first error of each top-level prompt in the order of `prompts`, each as a `Script error in <path>: ...` line of its own. A prompt's line names the prompt (`prompt '<name>': ...`), so two prompts with the same mistake give two different lines. These don't depend on each other, so none is a consequence of another. The first of the lines is the one `run` reports. `env` has at most one line, the first error found: another default that reads the failed one would fail for the same reason.

Options of `validate`:

| Flag | Description |
|------|-------------|
| `-a KEY=VALUE`, `--arg KEY=VALUE` | As for `run`. The arguments are used for every script given. Give the arguments the script is run with: a script whose `env` reads `{{ args.KEY }}` is invalid without `--arg KEY=...`, with the `Script error` that `run` reports for it (`env.<DEFAULT>: template error: args has no key 'KEY'; pass it with --arg KEY=VALUE`). An `--arg` without `=` is reported, as by `run`, for each script that gets as far as that check. |
| `-q`, `--quiet` | Print nothing for a valid script. An invalid one keeps its report and its `invalid` line. |
| `--traceback` | As for `run`: the Python traceback of a `Plugin error` or a `Script error` is printed before it. |
| `-h`, `--help` | Print the `validate` usage and exit with status 0. |

`validate` exits with status 0 when every script is valid, and with status 1 when any is invalid, after all of them have been checked. A command line without a script, or with an unknown option, prints usage and exits with status 2. An unexpected error (see [Errors while the script runs](#errors-while-the-script-runs)) ends the command where it happens, with the traceback and status 70, and the scripts after it aren't checked. Its report names the script that was being checked, in a line `  while checking <path>` before the traceback, since no `invalid` line follows. An interrupt prints `Interrupted`.

**What a valid script may still do.** With the same arguments, plugins and environment, `validate` finds a script valid exactly when `run` gets past loading it: a valid script doesn't end `run` with one of the load errors and status 1 (a closed stdout aside, which is about the command line and not the script), and an invalid one does. The environment is part of that, because a variable of the process's environment replaces the `env` default of the same name, which is then never rendered. A valid script can still fail once it runs, with status 3 (see [Errors while the script runs](#errors-while-the-script-runs)). `validate` doesn't catch:
- a template that is rendered during the run: `attach.prepare`, `attach.spawn`, and every templated field of a step, a block or a block's prompts. That includes an `{{ args.KEY }}` that only they read, so such a script is valid without the argument and fails in `run` without it (`Run failed in <path>: template error: args has no key 'KEY'; pass it with --arg KEY=VALUE`);
- a `spawn` that renders to no command. `run` renders a `spawn` that doesn't read `env` before `prepare`, but reports it as a failed run, not as a load error;
- an `env` default that waits for `attach.prepare` (see [The environment in templates](#the-environment-in-templates)) and can't be rendered after it;
- a block's `sendEach` collection, an `assert` or `after` that renders to an invalid regex;
- anything the device does: a prompt that never comes, a failed command, a rejected login.

### Output

Autobot writes to two streams, and each carries one kind of text:

| Stream | Carries |
|--------|---------|
| stdout | The session's output: everything read from the spawned process, with ANSI escape sequences removed (see [ANSI escape sequences](#ansi-escape-sequences)). During a run nothing else is written to it. `autobot schema` and the help also print to stdout; `autobot validate` prints nothing to it. |
| stderr | Autobot's own messages: the `>> ` progress lines, the CLI's error reports and tracebacks, the `valid` and `invalid` lines of `autobot validate`. |

So `autobot script.yaml > device.log` keeps the device's transcript and shows what Autobot did, and `2> run.log` does the opposite. The output of `attach.prepare` is the script's own: it inherits both streams, also when a shell sources it to read back its environment. Autobot prints no value of the environment of its own accord: the `prepare` line shows counts only, and an error about `env` names a variable, never its value. A value that a template puts into a command, a `spawn` or any other text is part of that text, though, and is printed wherever the text is: `>> attach: <spawn>` and `>> cmd: <line>` show it as rendered, and so does an error that quotes it (`The command was not found or was not executable: <command>`).

When stdout and stderr are the same terminal, pipe or file (a terminal with nothing redirected, `2>&1`, `&> run.log`), their lines interleave, and the session's output seldom ends with a line break: a prompt such as `admin@sonic:~$ ` leaves its line open. A message would then continue that line. So where the two streams are one and the session's output has left a line open, Autobot writes a line break to stderr before the message, and every message starts at the first column:

```
admin@sonic:~$ 
>> cmd: show version
show version
SONiC Software Version: ...
```

The line break goes to stderr, never to stdout, and none is written when the two streams are different or the session's output ended its line. So stdout is the same transcript however the streams are set up.

**The session's output is never restyled.** It is written as the device sent it, less the escape sequences: not wrapped, cut, reflowed or highlighted. The one exception is a character that stdout's encoding can't represent, e.g. `→` with `PYTHONIOENCODING=ascii` or a C locale: it is written as its backslash escape (`\u2192`), so the echo never fails the step that was reading. A stdout that can represent the character, such as a UTF-8 one, gets it unchanged. Text in it that looks like markup or an emoji code (`[bold]`, `[/]`, `:warning:`) is printed as it is, and no style is ever added to it.

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
| `>> register: vars.<name>`, `>> script: writing to <file>`, `>> script: executing <file>`, `>> script: cleaned up <file>`, `>> prepare: environment: <N> set, <M> unset` | detail | by `register`, by an embedded script, and when `attach.prepare` has set or unset environment variables: the counts, never a name or a value |
| `>> error ignored: <message>`, `>> breakout error (<type>): <message>`, `>> block breakout error (<type>): <message>`, `>> close error (<type>): <message>`, `>> script: interrupt sent: ^C`, `>> script: cleanup of <file> failed (<type>): <message>`, `>> prepare: environment not read: <reason>` (see [`prepare` as an rc script](#prepare-as-an-rc-script)) | warning | for a failure the run goes on from; `<type>` is the exception's class name |
| `>> step failed (<type>): <message>`, `>> step interrupted` | failure | when an error, or an interrupt, leaves the step it was raised in. A failure that the step itself handles never leaves it and prints no such line: an ignored one prints `error ignored`. The line is printed once, by the innermost step, at that step's level and at once, before any breakout runs; it can't wait to see how far the error goes, since the breakouts run as the error passes. So it is also printed for an error that something further out then stops: a failing step of a breakout prints it, before the breakout's own `breakout error` line, and so does a step run by a plugin step that catches the error from `ctx.run_steps(...)`, after which the run goes on |

**Nesting.** The steps of a block and of a called function are indented under the line that starts them, two spaces a level, for up to eight levels: a step nested deeper is printed at the eighth level, so that deep nesting, or functions that call each other without end, doesn't push the lines off the screen. The indentation comes after the marker, which stays in the first column:

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

**The CLI's reports** have no marker: the load errors, `Run failed in ...`, `Interrupted` and `Unexpected error in ...` (see below). They start at the first column with what happened, then `: ` and the message. The lines `<path>: valid` and `<path>: invalid` of `autobot validate` have no marker either.

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
| the `valid` and the `invalid` of `autobot validate`, after the path, which is plain | green, and bold red |
| the `at` and `called from` labels of a report | dim |

Only bold, dim and four of the terminal's own eight colors are used (SGR 1, 2 and 31 to 34: red, green, yellow and blue), never a fixed RGB value or a background color, so the terminal's theme decides the exact colors and keeps them readable on a dark and on a light background.

The messages are styled when stderr is a terminal whose `TERM` isn't `dumb` or `unknown`. Otherwise they are plain text without any escape sequence, so a pipe or a file gets a clean log. Two environment variables change that, each when set to a non-empty value, and no other does (`TTY_COMPATIBLE`, which rich reads on its own, has no effect):
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
  (run with --traceback for details)
```

`<path>` is the script file as given on the command line, shown as in every report (see [The script's path in a report](#cli)), and `<reason>` is the error's message. The step that failed has said so already, where it failed in the log (`>> step failed (<type>): <message>`, see [Output](#output)); the report comes last, after the breakouts. These are the failed runs:

| Failure | Exception | `<reason>`, e.g. |
|---------|-----------|------------------|
| A command failure that `ignore_error` doesn't cover: a non-zero exit code, an `assert` where no pattern matches, an embedded-script upload mismatch | `StepFailure` (`autobot.steps`), a `RunError` | `command returned exit code 1`, `assertion failed: expected ['version 5\\.']` |
| An `errors` pattern match | `CommandError` (`autobot.session`), a `RunError` | `command error: % Invalid input` |
| A `sendEach` prompt out of responses | `RunError` | `prompt 'login': responses exhausted` |
| A failed `attach.prepare` | `RunError` | `prepare script failed with exit code 3` |
| A template error at run time, an `assert` or `after` that renders to an invalid or empty regex, an `attach.spawn` that renders to no command, an `env` default that can't be rendered once `attach.prepare` has run, a block's `sendEach` collection that can't be resolved | `ScriptError` | `template error: 'dict object' has no attribute 'image'`, `template error: args has no key 'host'; pass it with --arg host=VALUE` |
| A timeout: waiting for a prompt, an `after` pattern, the `$?` result or the spawned process's first output | `TimeoutError` | `timed out after 30.0s waiting for a shell prompt ('sh')` |
| A closed connection | `EOFError` | `connection closed while waiting for a shell prompt ('sh')` |
| A `spawn` command that isn't found, or a process that can't be terminated when the session is closed | `pexpect.ExceptionPexpect` | `The command was not found or was not executable: sssh.` |
| An operating-system error on the session's pty or on the temp file of the `prepare` script, e.g. a full disk | `OSError` | `[Errno 28] No space left on device` |
| Text that can't be encoded for the `prepare` script's file, e.g. the lone surrogate a non-UTF-8 byte of an `--arg` becomes | `UnicodeError` | `'utf-8' codec can't encode character '\udcff' in position 5: surrogates not allowed` |
| Functions that call each other without end | `RecursionError` | `functions call each other too deeply (maximum recursion depth exceeded)` |

The last three rows are built-in classes that a bug can raise as well as the environment. Two rules keep one from hiding the other:
- The report of an `OSError`, `UnicodeError` or `RecursionError` ends with the line `  (run with --traceback for details)`, unless `--traceback` is given. A `TimeoutError`, though it is an `OSError`, is not one of them, and neither is any other row of the table.
- When a plugin's own code raises one of the three, it is not a failed run but an unexpected error in the plugin, like any other exception of the plugin (see below). The plugin's own code raised it when the step that was running is a plugin step and the traceback has no frame of Autobot below the call of the plugin's `execute`. An `OSError` that Autobot's session raises while the plugin uses it (`ctx.session.sendline(...)` on a pty that is gone) has such a frame, and is a failed run.

A `RecursionError` is reported as `functions call each other too deeply (...)` when a `call` step was running, in the step itself or among its callers. Otherwise the reason is the error's own message.

`RunError` and `ScriptError` are in `autobot.types`. `RunError` is a `RuntimeError` and `ScriptError` a `ValueError`, so code that catches those catches them too; `EnvError` is a `ScriptError`. Where this document says an error is a `ValueError` with a `template error: ...`, `assert: ...`, `after: ...`, `attach.spawn ...` or `prompt '<name>': sendEach ...` message, it is a `ScriptError`; the `RuntimeError` of a `sendEach` prompt (`responses exhausted`, `no response available`) and of a failed `prepare` is a `RunError`. A message that has no text is reported as the exception's type name. A plugin reports a failure of this kind by raising `RunError` (or `StepFailure`) for what the device did or `ScriptError` for a bad value in the script; a `TimeoutError`, an `EOFError` or a `pexpect.ExceptionPexpect` that it raises or lets through counts as the session's and is a failed run too.

The `at` line names the step that was running: its path in the script, and in parentheses what it is.
- The path is the list the step is in and its index, counted from 0. The lists are `script`, `attach.script`, `attach.breakout`, `fn.<name>.script`, and for the block at `<path>`, `<path>.block.enter`, `<path>.block.script` and `<path>.block.breakout`. So the second step of the main script of a block that is the fourth step of `script` is `script.3.block.script.1`. The steps a plugin builds in code and runs with `ctx.run_steps(...)` are in no list of the script: they are named after the plugin step that runs them, `<plugin step's path>.<key>.<index>`.
- A `cmd` shows the first non-blank line of the command as written in the script, not rendered, cut after 72 characters with `...`; for a list, the first item and ` (+<N> more)`. A `call` shows the function's name and a `block` the block's name, e.g. `(call: is_system_running)`, `(block: Host Console)`. Every other step shows only its key: `(line)`, `(control)`, `(sleep)`, `(return)`, and a plugin step its plugin key. A `line` never shows its text: it may be a password, which the session doesn't echo.
- A failure outside any step has no `at` line: a failed `prepare`, an `env` default that can't be rendered after it, a failed spawn wait, a `spawn` that renders to no command, a session that can't be closed.

A `called from` line follows for each step that was running the failing one through a `call` (or a plugin step that runs steps), nearest first: a step of a function is at `fn.<name>.script.<i>` wherever it is called from. At most five are listed, then `  ... and <N> more callers`. A block is not listed, since it is part of the path.

**An interrupt** (Ctrl-C, a `KeyboardInterrupt`) ends the run like any error: the breakouts run and the session is closed, as described above. An interrupt during a breakout ends that breakout; the session is still closed. Then the CLI prints `Interrupted`, with the `at` and `called from` lines of the step that was running, and the process ends from `SIGINT` itself: it restores the signal's default action and sends itself the signal. A shell reports that as status 130, and a shell loop around `autobot` stops as it does for any command that Ctrl-C kills, so the next iteration doesn't start; a parent that reads the wait status sees a process killed by `SIGINT`, not an exit status. Only if the signal doesn't end the process (it is blocked) does the CLI exit with status 130. There is no traceback. An interrupt before the run, e.g. while the script is loaded, prints `Interrupted` alone.

**An unexpected error** is any other exception: a bug in Autobot or in a plugin, not something the script's author can fix. A plain `ValueError` or `RuntimeError` is one, an `OSError`, `UnicodeError` or `RecursionError` from a plugin's own code is one (see above), and so is a `PluginError` raised while a script runs (the CLI reports a `PluginError` as a load error only from discovery). The CLI says so in one line, adds the `at` and `called from` lines when a step was running, prints the Python traceback and exits with status 70:

```
Unexpected error in Autobot: this is a bug, not a problem with the script. Please report it with the traceback below.
  at script.1 (cmd: show version)
Traceback (most recent call last):
  ...
KeyError: 'x'
```

When a plugin step was running, the first line names the plugin instead, whether the step that failed is the plugin step itself or a step the plugin runs with `ctx.run_steps(...)` (the nearest plugin step, if one runs another). A step that a plugin builds can fail in ways no step of a script can, e.g. a `call` to a function that isn't defined: `Unexpected error in plugin '<key>': this is a bug in the plugin, not in the script. Please report it to the plugin's author with the traceback below.` An exception outside a run, while the script is loaded or in `autobot schema`, is reported the same way, without the `at` line; in `autobot validate`, with the line `  while checking <path>` in its place (see [Checking scripts with `validate`](#checking-scripts-with-validate)). An exception that a validator of a plugin's model raises while a script is validated, other than the `ValueError` and `AssertionError` that make a validation error, is one of these. `SystemExit` is not caught.

With `--traceback`, the Python traceback of a failed run, of an interrupt, and of a `Script error` or `Plugin error` is printed as well, before the report; in `autobot validate`, before each `Script error` line. The report is the same as without the flag, less the line that points to the flag, and so is the way the process ends.

### Exit status

| Status | Meaning |
|--------|---------|
| 0 | The run completed; `autobot validate` found every script valid; `autobot schema` printed the schema; `-h` printed the help |
| 1 | The script couldn't be loaded (the load errors above), and nothing ran; `autobot validate` found a script invalid, or a `Plugin error`. Also `autobot` with no arguments, and a `Plugin error` or a missing schema in `autobot schema` |
| 2 | A malformed command line |
| 3 | The run failed. `attach.prepare` or the session may have run, and the breakouts have run if the session got past the spawn wait |
| 70 | An unexpected error: a bug in Autobot or in a plugin |
| 130 | Interrupted. The process ends from `SIGINT`, which a shell reports as 130; it isn't an exit status of the process itself (see above) |
