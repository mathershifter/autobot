# Autobot — Console Robot Script Engine

Autobot executes YAML-defined scripts against remote consoles via pexpect. It handles login negotiation, multi-hop session management, and cleanup automatically.

## Architecture

```
YAML script → pydantic validation → ScriptRunner → Session (pexpect) → remote console
```

- **YAML script** defines environment, credentials, prompt patterns, functions, attach config, and a step-based script.
- **Pydantic models** (`ScriptConfig` and subtypes) validate and parse the YAML against `autobot.schema.json`.
- **ScriptRunner** renders Jinja2 templates, dispatches steps, and manages context/breakout stacks.
- **Session** wraps pexpect, handles prompt detection and credential cycling.

## YAML Script Structure

All fields validated by pydantic against `autobot.schema.json`.

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `version` | yes | Semver string (e.g. `0.1.0`) |
| `env` | no | String key-value pairs, accessible as `{{ env.KEY }}` |
| `vars` | no | Arbitrary objects, accessible as `{{ vars.KEY }}` |
| `prompts` | no | Named prompt/response definitions for interactive sessions |
| `fn` | no | Named functions (reusable command + assert pairs) |
| `attach` | yes | Session spawn and lifecycle config |
| `script` | yes | Ordered list of steps to execute |

### `prompts`

Each prompt has:
- `name` — identifier
- `expect` — list of regex patterns to match against session output
- `send` — optional list of Jinja2 templates to send when a pattern matches. If omitted, matching this prompt means "we have a shell prompt" and the pending `cmd` is sent.
- `retry` — optional; how many times to cycle through the send values (supports Jinja2, e.g. `"{{ len(vars.creds) }}"`)

Send values are rendered with `i` set to the current retry index, enabling credential cycling:
```yaml
send:
  - "{{ vars.creds[i].username }}"
  - "{{ vars.creds[i].password }}"
```

When the send list has fewer entries than the expect list, the last send value is reused for remaining patterns.

### `fn`

Named functions callable from `call` steps:
```yaml
fn:
  is_system_running:
    cmd: systemctl is-system-running --wait
    assert:
      - running
      - degraded
```

### `attach`

| Field | Required | Description |
|-------|----------|-------------|
| `spawn` | yes | Command to spawn via pexpect (e.g. `ssh host`, `telnet host port`) |
| `timeout` | no | Default session timeout (duration) |
| `env` | no | Environment variables for the spawned process |
| `script` | no | Steps to run immediately after spawn (before main script) |
| `breakout` | no | Steps to run in `finally` after the main script (cleanup/disconnect) |

The attach lifecycle:
1. `pexpect.spawn(attach.spawn)` — waits for initial output
2. `attach.script` steps execute (e.g. jump-host commands)
3. Main `script` steps execute
4. Wait for final prompt
5. `attach.breakout.script` executes (best-effort, errors logged to stderr)
6. Session closed

## Step Types

### `cmd` — Send command(s) to the shell

Waits for a prompt, sends the command, optionally asserts output.

```yaml
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
```

`cmd` accepts a string or list of strings. Each line waits for a prompt before sending.

### `sleep` — Pause execution

```yaml
- sleep: 60s
```

### `call` — Invoke a named function

```yaml
- call: is_system_running
```

### `context` / `exit` — Scoped session with cleanup

Push a named context with a breakout script. When `exit` is called, the breakout runs.

```yaml
- context:
    name: bootloader
    breakout:
      script:
        - cmd: reset
# ... do work ...
- exit: bootloader   # runs the breakout script
```

### `block` — Named group of steps

```yaml
- block:
    name: Install SONiC
    script:
      - cmd: sonic-installer install -y image.swi
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

All step types except `sleep` and `context` support:

| Field | Description |
|-------|-------------|
| `when` | Expect regex — wait for this pattern before executing |
| `delay_before` | Duration to wait before the step |
| `delay_after` | Duration to wait after the step |
| `timeout` | Override default timeout for this step |

`line` and `return` steps do not support `timeout`.

## Duration Format

Durations accept a bare number (seconds) or a string with a unit suffix:
- `5`, `5s` — 5 seconds
- `500ms` — 500 milliseconds
- `2m` — 2 minutes
- `1h` — 1 hour

## Jinja2 Templating

All string values in `cmd`, `assert`, `attach.spawn`, and prompt `send`/`retry` fields support Jinja2 templates. Available context:

| Variable | Source |
|----------|--------|
| `env` | `env` section of the YAML |
| `vars` | `vars` section of the YAML |
| `args` | CLI `--arg KEY=VALUE` arguments |

Built-in functions: `len`, `range`, `int`, `str`.

## Prompt Handling (`get_prompt`)

`get_prompt()` only detects and navigates to a shell prompt — it does not send commands. The caller is responsible for sending the command via `sendline()` after `get_prompt()` returns.

The prompt engine polls the session output in 5-second intervals:
1. If a prompt with no `send` matches → return (cli prompt reached)
2. If a prompt with `send` values matches → send the appropriate response and continue waiting
3. On 5-second timeout with no match → send an empty newline to solicit a prompt
4. On overall timeout → raise `TimeoutError`

This handles idle consoles that need a return press to display a prompt.

## Lint (`--validate-only`)

Checks for issues beyond structural validation:
- Undefined `env.X` references in templates
- Bare `UPPER_CASE` template expressions missing `env.` prefix
- Common typos in env keys, function asserts, and credentials
- Null `block`/`context` values from YAML indentation errors
- Empty or trailing-quote `cmd` values

## CLI

```
autobot.py <script.yaml> [--arg KEY=VALUE ...] [--validate-only]
```

- `script` — path to the YAML script file
- `--arg` — pass arguments accessible as `{{ args.KEY }}`
- `--validate-only` — run lint + pydantic validation, exit with code 1 on errors
