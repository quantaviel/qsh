"""Run fixed host programs and OS pipelines without invoking a shell."""

import os
from dataclasses import dataclass
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

from .output import OUTPUT_LIMIT, TRUNCATED

PROGRAM_NAMES = ("ls", "cat", "pwd", "grep", "ps")
TIMEOUT = 5.0
BYTE_LIMIT = OUTPUT_LIMIT * 4


@dataclass(frozen=True)
class Result:
    output: str
    status: int
    interrupted: bool = False


class Programs:
    def __init__(self):
        # Never search the conversation's cwd or a user-supplied PATH.
        self.paths = {name: shutil.which(name, path=os.defpath) for name in PROGRAM_NAMES}

    def execute(self, stages: list[list[str]], cwd: Path, *, deadline: float | None = None) -> Result:
        commands = []
        # Validate the entire pipeline before starting any process.
        for name, *args in stages:
            if name not in self.paths:
                raise ValueError(f"未知命令：{name}。支持 cd、{'、'.join(self.paths)}。")
            executable = self.paths[name]
            if executable is None:
                return Result(f"系统未安装命令：{name}", 127)
            commands.append([executable, *args])
        env = {"PATH": os.defpath, "HOME": str(Path.home())}
        for key in ("LANG", "LC_ALL", "LC_CTYPE"):
            if key in os.environ:
                env[key] = os.environ[key]
        output = bytearray()
        notice = ""
        interrupted_status = 125
        processes = []
        deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + TIMEOUT)
        try:
            with selectors.DefaultSelector() as selector:
                previous = None
                for command in commands:
                    process = subprocess.Popen(
                        command, cwd=cwd, env=env,
                        stdin=previous if previous is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                    )
                    processes.append(process)
                    if previous is not None:
                        previous.close()
                    previous = process.stdout
                    selector.register(process.stderr, selectors.EVENT_READ)
                selector.register(processes[-1].stdout, selectors.EVENT_READ)
                # Intermediate stdout streams flow directly through kernel pipes;
                # only final stdout and each stage's stderr are captured here.
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("pipeline", TIMEOUT)
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fileobj.fileno(), min(4096, BYTE_LIMIT + 1 - len(output)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > BYTE_LIMIT:
                            notice = TRUNCATED
                            break
                    if notice:
                        break
                if not notice:
                    for process in processes:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            interrupted_status = 124
            notice = f"\n（管道执行超时，已全部终止；限时 {TIMEOUT:g} 秒）"
        finally:
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS can reject signalling an already-exited group.
                    # Reap its leader; if still alive, terminate it directly.
                    if process.poll() is None:
                        process.kill()
            for process in processes:
                process.wait()
                process.stdout.close()
                process.stderr.close()

        text = bytes(output[:BYTE_LIMIT]).decode("utf-8", errors="replace").rstrip("\n")
        if notice:
            return Result(text[: OUTPUT_LIMIT - len(notice)] + notice,
                          interrupted_status, interrupted=True)
        # Standard shell semantics: a pipeline's status is its last stage's
        # status, irrespective of diagnostic messages from earlier stages.
        status = processes[-1].returncode
        if status < 0:
            status = 128 - status
        failures = []
        for stage, process in zip(stages, processes):
            code = process.returncode
            # grep=1 means no match. SIGPIPE upstream is normal when a later
            # stage such as grep -q finishes before consuming all input.
            if code and not (stage[0] == "grep" and code == 1) and code != -signal.SIGPIPE:
                failures.append(f"{stage[0]}={code}")
        if failures:
            suffix = "\n（退出码：" + "，".join(failures) + "）"
            return Result(text[: OUTPUT_LIMIT - len(suffix)] + suffix, status)
        if stages[-1][0] == "grep" and processes[-1].returncode == 1:
            return Result(text or "（无匹配）", status)
        return Result(text or "（无输出）", status)
