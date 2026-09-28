from __future__ import annotations

import base64
import hashlib
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import SHELL_ENV, SentLog
from conftest import run_vars as run

import autobot.steps
from autobot.session import CommandError, Session

GENERIC = r"[>#\$] ?$"


def prompts(pattern: str) -> list[dict[str, Any]]:
    return [{"name": "sh", "expect": [pattern]}]


@pytest.fixture
def tmp_path_hex(monkeypatch: pytest.MonkeyPatch) -> Path:
    fixed = uuid.uuid4()
    monkeypatch.setattr(autobot.steps.uuid, "uuid4", lambda: fixed)
    return Path(f"/tmp/_autobot_{fixed.hex}")


def test_generic_prompt_register():
    # the old heredoc upload desynced intermittently (PS2 '> ' matched the
    # prompt regex), so repeat to make a regression reliably visible
    steps: list[dict[str, Any]] = []
    for i in range(6):
        script = f"#!/bin/sh\necho one{i}\nif true; then\n  echo two{i}\nfi\nexit 0\n"
        steps.append({"cmd": script, "register": f"out{i}"})
    steps.append({"cmd": "echo next", "register": "next"})
    out = run(steps, prompts=prompts(GENERIC))
    for i in range(6):
        assert out[f"out{i}"] == f"one{i}\ntwo{i}"
    assert out["next"] == "next"


def test_generic_prompt_rc_failure():
    with pytest.raises(RuntimeError, match="exit code 3"):
        run([{"cmd": "#!/bin/sh\necho x\nexit 3\n"}], prompts=prompts(GENERIC))


def test_generic_prompt_rc_ignored():
    out = run(
        [
            {"cmd": "#!/bin/sh\necho partial\nexit 3\n", "ignore_error": True, "register": "out"},
            {"cmd": "echo next", "register": "next"},
        ],
        prompts=prompts(GENERIC),
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
    out = run([{"cmd": script, "register": "out"}], prompts=prompts(prompt))
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
        run([{"cmd": "#!/bin/sh\necho hi\n"}], attach_env={**SHELL_ENV, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
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


# -- P2-07..15: embedded scripts (SPEC.md:140-172) ---------------------------


UPLOAD_PREFIX = "(umask 077; printf %s "


def uploaded_chunks(sent: SentLog) -> list[str]:
    return [
        line[len(UPLOAD_PREFIX) :].split(" ", 1)[0]
        for line in sent.lines()
        if line.startswith(UPLOAD_PREFIX)
    ]


def assert_removed(tmp: Path) -> None:
    assert not tmp.exists()
    assert not Path(f"{tmp}.b64").exists()


def fake_wc_env(tmp_path: Path) -> dict[str, str]:
    fake = tmp_path / "wc"
    fake.write_text("#!/bin/sh\ncat >/dev/null\necho 1\n")
    fake.chmod(0o755)
    return {**SHELL_ENV, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_p2_07_upload_chunks_at_most_512(sent: SentLog):
    """SPEC.md:165: base64 is sent in single-line chunks of at most 512 chars."""
    script = "#!/bin/sh\n# " + "abcdefgh" * 200 + "\necho ok\n"
    b64 = base64.b64encode(script.encode()).decode()
    assert len(b64) > 3 * 512
    out = run([{"cmd": script, "register": "out"}])
    assert out["out"] == "ok"
    chunks = uploaded_chunks(sent)
    assert all(len(c) <= 512 for c in chunks)
    assert len(chunks) == math.ceil(len(b64) / 512)
    assert "".join(chunks) == b64


@pytest.mark.parametrize(
    "script", ["#!/bin/sh\necho ok\n", "#!/bin/sh\necho ok"], ids=["newline", "no-newline"]
)
def test_p2_08_upload_trailing_newline_preserved_once(sent: SentLog, script: str):
    """SPEC.md:165: the rendered script is uploaded with one trailing newline."""
    run([{"cmd": script}])
    data = base64.b64decode("".join(uploaded_chunks(sent))).decode()
    assert data.endswith("\n")
    assert not data.endswith("\n\n")


def test_p2_09_script_mode_700_and_staging_umask_077():
    """SPEC.md:166: files created under umask 077; the script is mode 700."""
    out = run([{"cmd": '#!/bin/sh\nstat -c %a "$0" "$0.b64"\n', "register": "out"}])
    assert out["out"] == "700\n600"


def test_p2_10_embedded_script_is_rendered():
    """SPEC.md:162: embedded scripts are Jinja2 templates."""
    out = run([{"cmd": "#!/bin/sh\necho {{ vars.x }}\n", "register": "out"}], vars={"x": "hi"})
    assert out["out"] == "hi"


def test_p2_11_embedded_assert_failure_cleans_up(tmp_path_hex: Path):
    """SPEC.md:162, 171: assert works; files removed when it fails."""
    with pytest.raises(RuntimeError, match="assertion failed"):
        run([{"cmd": "#!/bin/sh\necho hi\n", "assert": "nomatch"}])
    assert_removed(tmp_path_hex)


def test_p2_12_byte_count_mismatch_fails_step(tmp_path_hex: Path, tmp_path: Path):
    """SPEC.md:167: a byte-count mismatch is a step failure."""
    with pytest.raises(RuntimeError, match="script upload .* failed"):
        run([{"cmd": "#!/bin/sh\necho hi\n"}], attach_env=fake_wc_env(tmp_path))
    assert_removed(tmp_path_hex)


def test_p2_13_byte_count_mismatch_ignorable(tmp_path_hex: Path, tmp_path: Path):
    """SPEC.md:167: ignore_error swallows a byte-count mismatch."""
    out = run(
        [
            {"cmd": "#!/bin/sh\necho hi\n", "ignore_error": True},
            {"cmd": "echo next", "register": "next"},
        ],
        attach_env=fake_wc_env(tmp_path),
    )
    assert out["next"] == "next"
    assert_removed(tmp_path_hex)


def test_p2_14_cleanup_timeout_uses_shorter_step_timeout(tmp_path_hex: Path):
    """SPEC.md:172: cleanup waits at most min(10s, step timeout)."""
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            run([{"cmd": "#!/bin/sh\nsleep 30\n", "timeout": "2s"}])
        # 2s step timeout + at most 2s cleanup, not the 10s cleanup cap
        assert time.monotonic() - start < 6
    finally:
        tmp_path_hex.unlink(missing_ok=True)
        Path(f"{tmp_path_hex}.b64").unlink(missing_ok=True)


def test_p2_15_embedded_errors_patterns_apply(tmp_path_hex: Path):
    """SPEC.md:101, 162: top-level errors apply to embedded script output."""
    with pytest.raises(CommandError, match="command error: % bad"):
        run([{"cmd": "#!/bin/sh\necho '% bad'\n"}], errors=["% .*"])
    assert_removed(tmp_path_hex)
