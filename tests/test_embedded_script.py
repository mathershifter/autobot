from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import autobot.steps
from autobot.models import Config
from autobot.runner import Runner
from autobot.session import Session

BASH = "bash --norc --noprofile -i"
GENERIC = r"[>#\$] ?$"


@pytest.fixture
def tmp_path_hex(monkeypatch: pytest.MonkeyPatch) -> Path:
    fixed = uuid.uuid4()
    monkeypatch.setattr(autobot.steps.uuid, "uuid4", lambda: fixed)
    return Path(f"/tmp/_autobot_{fixed.hex}")


def run(
    script: list[dict[str, Any]],
    prompt: str = r"PROMPT\$ ",
    path: str | None = None,
) -> dict[str, Any]:
    for step in script:
        step.setdefault("timeout", "10s")
    cfg = Config.model_validate(
        {
            "autobot": "2026-08",
            "prompts": [{"name": "sh", "expect": [prompt]}],
            "attach": {
                "spawn": BASH,
                "timeout": 10,
                "env": {"TERM": "dumb", "PS1": "PROMPT$ ", "PATH": path or os.environ["PATH"]},
                "script": [{"return": 1}],
            },
            "script": script,
        }
    )
    Runner(cfg, {}).run()
    return cfg.vars


def test_generic_prompt_register():
    # the old heredoc upload desynced intermittently (PS2 '> ' matched the
    # prompt regex), so repeat to make a regression reliably visible
    steps: list[dict[str, Any]] = []
    for i in range(6):
        script = f"#!/bin/sh\necho one{i}\nif true; then\n  echo two{i}\nfi\nexit 0\n"
        steps.append({"cmd": script, "register": f"out{i}"})
    steps.append({"cmd": "echo next", "register": "next"})
    out = run(steps, prompt=GENERIC)
    for i in range(6):
        assert out[f"out{i}"] == f"one{i}\ntwo{i}"
    assert out["next"] == "next"


def test_generic_prompt_rc_failure():
    with pytest.raises(RuntimeError, match="exit code 3"):
        run([{"cmd": "#!/bin/sh\necho x\nexit 3\n"}], prompt=GENERIC)


def test_generic_prompt_rc_ignored():
    out = run(
        [
            {"cmd": "#!/bin/sh\necho partial\nexit 3\n", "ignore_error": True, "register": "out"},
            {"cmd": "echo next", "register": "next"},
        ],
        prompt=GENERIC,
    )
    assert out["out"] == "partial"
    assert out["next"] == "next"


def test_old_heredoc_marker_and_special_chars():
    body = "hello $HOME `x` \\n \\\\ \"dq\" 'sq' ; & | < > # !"
    script = f"#!/bin/sh\ncat <<'AUTOBOT_SCRIPT_EOF'\n{body}\nAUTOBOT_SCRIPT_EOF\necho done\n"
    out = run([{"cmd": script, "register": "out"}])
    assert out["out"] == f"{body}\ndone"


@pytest.mark.parametrize("prompt", [r"PROMPT\$ ", GENERIC])
def test_script_round_trips_exactly(prompt: str):
    long_line = "# " + "".join(chr(33 + i % 94) for i in range(5000)).replace("{", "(")
    script = "\n".join(
        [
            "#!/usr/bin/env python3",
            "import hashlib, sys",
            "print(hashlib.sha256(open(sys.argv[0], 'rb').read()).hexdigest())",
            "X = r'''",
            "AUTOBOT_SCRIPT_EOF",
            "$HOME ${PATH} $(id) `id` \\ \\\\ \\n \"dq\" 'sq' \t tab é ∑",
            "'''",
            long_line,
            "",
        ]
    )
    assert not any(t in script for t in ("{{", "{%", "{#"))
    assert len(script) > 4 * autobot.steps.SCRIPT_CHUNK
    out = run([{"cmd": script, "register": "out"}], prompt=prompt)
    assert out["out"] == hashlib.sha256(script.encode()).hexdigest()


def test_temp_file_removed_on_success(tmp_path_hex: Path):
    run([{"cmd": "#!/bin/sh\necho hi\n"}])
    assert not tmp_path_hex.exists()
    assert not Path(f"{tmp_path_hex}.b64").exists()


def test_temp_file_removed_on_script_failure(tmp_path_hex: Path):
    with pytest.raises(RuntimeError, match="exit code 1"):
        run([{"cmd": "#!/bin/sh\nexit 1\n"}])
    assert not tmp_path_hex.exists()
    assert not Path(f"{tmp_path_hex}.b64").exists()


def test_temp_file_removed_on_upload_failure(tmp_path_hex: Path, tmp_path: Path):
    fake = tmp_path / "base64"
    fake.write_text("#!/bin/sh\necho 'base64: broken' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(RuntimeError, match="script upload .* failed"):
        run([{"cmd": "#!/bin/sh\necho hi\n"}], path=f"{tmp_path}:{os.environ['PATH']}")
    assert not Path(f"{tmp_path_hex}.b64").exists()
    assert not tmp_path_hex.exists()


def test_cleanup_failure_does_not_mask_error(
    tmp_path_hex: Path, monkeypatch: pytest.MonkeyPatch
):
    orig = Session.sendline

    def sendline(self: Session, line: str = "") -> None:
        if line.startswith("rm -f"):
            raise OSError("boom")
        orig(self, line)

    monkeypatch.setattr(Session, "sendline", sendline)
    with pytest.raises(RuntimeError, match="exit code 4"):
        run([{"cmd": "#!/bin/sh\nexit 4\n"}])
    tmp_path_hex.unlink(missing_ok=True)
    Path(f"{tmp_path_hex}.b64").unlink(missing_ok=True)


def test_cleanup_failure_does_not_fail_step(
    tmp_path_hex: Path, monkeypatch: pytest.MonkeyPatch
):
    orig = Session.sendline

    def sendline(self: Session, line: str = "") -> None:
        if line.startswith("rm -f"):
            raise OSError("boom")
        orig(self, line)

    monkeypatch.setattr(Session, "sendline", sendline)
    out = run([{"cmd": "#!/bin/sh\necho ok\n", "register": "out"}])
    assert out["out"] == "ok"
    tmp_path_hex.unlink(missing_ok=True)
    Path(f"{tmp_path_hex}.b64").unlink(missing_ok=True)


def test_timeout_not_masked_and_cleanup_bounded(
    tmp_path_hex: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(autobot.steps, "SCRIPT_CLEANUP_TIMEOUT", 1.0)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        run([{"cmd": "#!/bin/sh\nsleep 30\n", "timeout": "4s"}])
    # 4s step timeout + 1s cleanup wait, not a second full step timeout
    assert time.monotonic() - start < 7
    tmp_path_hex.unlink(missing_ok=True)
    Path(f"{tmp_path_hex}.b64").unlink(missing_ok=True)
