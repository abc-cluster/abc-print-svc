"""How a build command is executed.

Both engines run one external command at a time — `thesis-assemble.sh`, or one
`quarto render` per output format — and need the same two things back: its exit
status and everything it printed, which the thesis engine parses for its check
report.

The default runs the command with subprocess, which is all a library caller or a
test needs. The service passes a runner built on abc-dispatch's `run_tool`
instead (see `abcprint.service`), because a dispatched run can be CANCELLED:
subprocess.run would let a cancelled build keep rendering for minutes, holding a
render slot nobody is waiting for, while run_tool stops the whole process group.
"""
from __future__ import annotations

import subprocess
from typing import Callable, Protocol


class TimedOut(Exception):
    """The command exceeded its timeout and was stopped."""


class Runner(Protocol):
    def __call__(self, cmd: list[str], *, cwd: str, env: dict,
                 timeout_s: float) -> tuple[int, str]: ...


def subprocess_runner(cmd: list[str], *, cwd: str, env: dict,
                      timeout_s: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise TimedOut()
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


DEFAULT: Callable[..., tuple[int, str]] = subprocess_runner
