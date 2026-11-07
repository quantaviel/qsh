"""Session working directory and the small supported command grammar."""

import os
from pathlib import Path
import shlex
from threading import Lock
import time

from .output import OUTPUT_LIMIT, bounded
from .programs import Programs, Result, TIMEOUT


def parse_commands(command: str) -> list[tuple[str | None, list[list[str]]]]:
    """Parse an AND/OR list of pipelines, preserving quoted operators."""
    commands = []
    stages = []
    connector = None
    start = index = 0
    quote = None
    escaped = False
    while index < len(command):
        char = command[index]
        operator = None
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif command[index:index + 2] in ("&&", "||"):
            operator = command[index:index + 2]
        elif char == "|":
            operator = char
        elif char in ";&<>":
            raise ValueError("仅支持 |、&&、||，不支持 ;、后台执行或重定向")
        if operator:
            args = shlex.split(command[start:index])
            if not args:
                raise ValueError("运算符两侧必须有命令")
            stages.append(args)
            if operator != "|":
                commands.append((connector, stages))
                stages = []
                connector = operator
            index += len(operator)
            start = index
        else:
            index += 1
    args = shlex.split(command[start:])
    if not args and (stages or commands):
        raise ValueError("运算符末尾缺少命令")
    if args:
        stages.append(args)
        commands.append((connector, stages))
    if any(len(pipeline) > 16 for _, pipeline in commands):
        raise ValueError("一条管道最多允许 16 个命令")
    if sum(len(pipeline) for _, pipeline in commands) > 64:
        raise ValueError("一次请求最多允许 64 个命令")
    return commands


class Shell:
    """One user's working directory in one conversation. Never calls chdir."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.programs = Programs()
        self._lock = Lock()

    def execute(self, command: str) -> str:
        with self._lock:
            try:
                commands = parse_commands(command)
                if not commands:
                    return "支持：ls、cat、cd、pwd、grep、ps；使用 |、&&、|| 组合命令。"
                # Check the complete expression before any command changes cwd
                # or starts a process, including branches that may be skipped.
                for _, stages in commands:
                    for name, *_ in stages:
                        if name == "cd":
                            if len(stages) != 1:
                                raise ValueError("cd 不能放入管道，可用 cd 目录 && 命令")
                        elif name not in self.programs.paths:
                            raise ValueError(f"未知命令：{name}")
                status = 0
                output = ""
                deadline = time.monotonic() + TIMEOUT
                for connector, stages in commands:
                    if connector == "&&" and status != 0 or connector == "||" and status == 0:
                        continue
                    if time.monotonic() >= deadline:
                        result = Result("（组合命令执行超时，已停止）", 124, interrupted=True)
                    else:
                        try:
                            if stages[0][0] == "cd":
                                result = Result(self.cd(stages[0][1:]), 0)
                            else:
                                result = self.programs.execute(stages, self.cwd, deadline=deadline)
                        except (OSError, ValueError, RuntimeError) as error:
                            result = Result(f"错误：{error}", 127 if isinstance(error, FileNotFoundError) else 1)
                    status = result.status
                    output = (output + ("\n" if output else "") + result.output)[:OUTPUT_LIMIT + 1]
                    if result.interrupted:
                        break
                return bounded(output)
            except (OSError, ValueError, RuntimeError) as error:
                return bounded(f"错误：{error}")

    def cd(self, args: list[str]) -> str:
        if args and args[0] == "--":
            args = args[1:]
        if len(args) > 1:
            raise ValueError("用法：cd [目录]")
        path = Path(args[0] if args else "~").expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        if not os.access(path, os.X_OK):
            raise PermissionError(str(path))
        self.cwd = path
        return str(path)
