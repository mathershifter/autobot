# Autobot

A YAML-driven console robot for automating interactive sessions over SSH, telnet, and serial consoles. Autobot handles prompt detection, credential cycling, multi-hop connections, and session cleanup.

## Install

```bash
pipx install git+https://github.com/mathershifter/autobot.git
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/mathershifter/autobot.git autobot script.yaml
```

## Quick Start

Create a script file `example.yaml`:

```yaml
version: 0.1.0

vars:
  creds:
    - username: admin
      password: password

prompts:
  - name: cli
    expect:
      - '\w+@[\w\-\.]+:[^\$]+'
  - name: login
    retry: "{{ len(vars.creds) }}"
    expect:
      - 'login:'
      - '(?:P|p)assword:'
    send:
      - "{{ vars.creds[i].username }}"
      - "{{ vars.creds[i].password }}"

attach:
  spawn: ssh {{ args.host }}
  timeout: 60s

script:
  - cmd: show version
  - cmd: show interfaces status
```

Run it:

```bash
./autobot.py example.yaml --arg host=myswitch.local
```

## Usage

```
autobot.py <script.yaml> [--arg KEY=VALUE ...] [--validate-only]
```

| Flag | Description |
|------|-------------|
| `--arg KEY=VALUE` | Pass arguments accessible as `{{ args.KEY }}` in templates |
| `--validate-only` | Validate the YAML and report issues without executing |

## Script Structure

```yaml
version: 0.1.0

env:                    # string key-value pairs for {{ env.KEY }}
  IMAGE_URL: https://...

vars:                   # arbitrary data for {{ vars.KEY }}
  creds:
    - username: admin
      password: secret

prompts:                # interactive prompt handlers
  - name: cli
    expect: ['\w+@\w+:.+']
  - name: login
    expect: ['login:', '(?:P|p)assword:']
    send: ['{{ vars.creds[i].username }}', '{{ vars.creds[i].password }}']
    retry: "{{ len(vars.creds) }}"

fn:                     # reusable functions
  wait_for_boot:
    cmd: systemctl is-system-running --wait
    assert: [running, degraded]

attach:                 # session lifecycle
  spawn: ssh jumphost
  script:               # post-connect setup
    - line: connect-to-device
    - return: 1
  breakout:             # cleanup (runs in finally)
    script:
      - cmd: logout
      - control: "]"
  timeout: 300s

script:                 # main steps
  - cmd: show version
  - call: wait_for_boot
```

## Step Types

### cmd

Send one or more commands. Waits for a prompt before each line.

```yaml
- cmd: show version
- cmd: show version
  assert: "SONiC Software Version"
  timeout: 30s
- cmd:
    - configure terminal
    - interface Ethernet1
    - shutdown
```

### sleep

Pause execution.

```yaml
- sleep: 60s
```

### call

Invoke a named function from `fn`.

```yaml
- call: wait_for_boot
```

### line

Send raw text without waiting for a prompt.

```yaml
- line: a dut attach device01
```

### return

Send empty newline(s).

```yaml
- return: 1
- return: 3
```

### control

Send control characters.

```yaml
- control: "]"           # Ctrl+]
- control: [a, x]        # Ctrl+A, Ctrl+X
```

### context / exit

Push a named context with a breakout script. Calling `exit` runs the breakout.

```yaml
- context:
    name: host-console
    breakout:
      script:
        - cmd: logout
        - control: [a, x]
# ... work inside context ...
- exit: host-console
```

### block

Group steps under a label.

```yaml
- block:
    name: Upgrade firmware
    script:
      - cmd: firmware-upgrade --apply
      - sleep: 30s
      - cmd: show firmware
```

## Common Step Options

Most steps support these optional fields:

```yaml
- cmd: risky-command
  when: "Are you sure?"     # wait for this pattern first
  delay_before: 2s          # pause before executing
  delay_after: 5s           # pause after executing
  timeout: 120s             # override default timeout
```

## Duration Format

```
5       # 5 seconds (bare number)
500ms   # milliseconds
30s     # seconds
2m      # minutes
1h      # hours
```

## Prompts

Prompts define how autobot recognizes and responds to interactive patterns.

A prompt with no `send` field is a **shell prompt** -- when matched, the pending command is sent:

```yaml
- name: cli
  expect:
    - '\w+@[\w\-\.]+:[^\$]+'
    - '(arista-)?bmc-boot=>'
```

A prompt with `send` is an **interactive prompt** -- autobot responds automatically:

```yaml
- name: login
  retry: "{{ len(vars.creds) }}"
  expect:
    - 'login:'
    - '(?:P|p)assword:'
  send:
    - "{{ vars.creds[i].username }}"
    - "{{ vars.creds[i].password }}"
```

The variable `i` is the retry index. With `retry: 2` and two credential sets, autobot tries the first pair, and if the login prompt reappears, tries the second.

## Templating

All string values support [Jinja2](https://jinja.palletsprojects.com/) templates:

```yaml
- cmd: wget -P /tmp {{ env.IMAGE_URL }}
- cmd: echo {{ args.message }}
```

Available context:

| Variable | Source |
|----------|--------|
| `env.*` | `env` section |
| `vars.*` | `vars` section |
| `args.*` | `--arg` CLI flags |

Built-in functions: `len`, `range`, `int`, `str`.

## Validation

Check a script for structural and lint errors without executing:

```bash
./autobot.py script.yaml --validate-only
```

Catches:
- Missing or misspelled `env` references
- YAML indentation issues with `block`/`context`
- Empty commands, trailing quotes
- Common typos

## Schema

The full JSON Schema is in [`autobot.schema.json`](autobot.schema.json).
