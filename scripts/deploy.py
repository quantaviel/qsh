#!/usr/bin/env python3
"""Install and manage qsh as an unprivileged Linux systemd service."""

import argparse
import os
from pathlib import Path
import platform
import pwd
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
UNIT_NAME = "qsh.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME


def unit_quote(value: str, *, argument: bool = False) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("路径或用户名不能包含控制字符")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if argument:
        value = value.replace("$", "$$")
    return f'"{value}"'


def render_unit(project: Path, env_file: Path, cwd: Path, user: str) -> str:
    if not user or user == "root" or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in user):
        raise ValueError("请使用 --user 指定运行机器人的普通用户，不能使用 root")
    if str(project).rstrip() != str(project) or str(project).endswith("\\"):
        raise ValueError("项目目录不能以空白或反斜杠结尾")
    argv = [str(project / ".venv/bin/python"), "-u", "-m", "qsh",
            "--env-file", str(env_file), "--cwd", str(cwd)]
    return "\n".join([
        "[Unit]", "Description=qsh QQ Shell (WebSocket)",
        "Wants=network-online.target", "After=network-online.target", "",
        "[Service]", "Type=simple", f"User={user}",
        # WorkingDirectory is a single literal path, not an argv list: systemd
        # does not strip surrounding quotes for this directive.
        "WorkingDirectory=" + str(project).replace("%", "%%"),
        "ExecStart=" + " ".join(unit_quote(arg, argument=True) for arg in argv),
        "Restart=always", "RestartSec=5", "TimeoutStopSec=30",
        "KillMode=control-group", "UMask=0077", "NoNewPrivileges=true",
        "StandardOutput=journal", "StandardError=journal", "SyslogIdentifier=qsh", "",
        "[Install]", "WantedBy=multi-user.target", "",
    ])


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def install(project: Path, env_file: Path, cwd: Path, user: str):
    # Render and validate all user input before touching the installed service.
    unit = render_unit(project, env_file, cwd, user)
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError("服务运行用户不能具有 root UID")
    if not cwd.is_dir():
        raise ValueError(f"初始工作目录不存在：{cwd}")
    if not env_file.is_file():
        raise ValueError(f"凭证文件不存在：{env_file}")
    runuser = shutil.which("runuser")
    if not runuser:
        raise ValueError("缺少 runuser，请安装 util-linux")
    # Python and pip run as the service user, never as root.
    as_user = [runuser, "-u", user, "--"]
    run(*as_user, "test", "-r", env_file)
    run(*as_user, "test", "-x", cwd)
    python = project / ".venv/bin/python"
    if not python.exists():
        run(*as_user, sys.executable, "-m", "venv", project / ".venv")
    run(*as_user, python, "-c", "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'")
    run(*as_user, python, "-m", "pip", "install", "-e", project)
    run(*as_user, python, "-m", "pip", "check")
    # Validate credentials using the same parser as qsh without printing them.
    check = (
        "import sys; from dotenv import dotenv_values; "
        "d=dotenv_values(sys.argv[1]); "
        "assert all(isinstance(d.get(k), str) and d[k].strip() "
        "for k in ('QQ_APPID','QQ_SECRET')), 'QQ_APPID/QQ_SECRET required in env file'"
    )
    run(*as_user, python, "-c", check, env_file)
    # Keep the venv executable path intact; resolving its symlink would bypass
    # the virtual environment and use the base interpreter.
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".service", dir=UNIT_PATH.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(unit)
    try:
        temporary.chmod(0o644)
        run("systemd-analyze", "verify", temporary)
        temporary.replace(UNIT_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", UNIT_NAME)
    run("systemctl", "restart", UNIT_NAME)
    print("已安装并启动 qsh.service；查看日志确认 QQ WebSocket 已就绪。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="部署 qsh 为 Linux systemd 服务")
    parser.add_argument("action", choices=("install", "start", "stop", "restart", "status", "logs", "uninstall", "render"))
    parser.add_argument("--user", default=os.environ.get("SUDO_USER") or os.environ.get("USER"), help="install/render 的服务运行用户；sudo 时默认原用户，不能为 root")
    parser.add_argument("--env-file", type=Path, default=PROJECT / ".env", help="install/render 的凭证文件，默认项目根目录/.env")
    parser.add_argument("--cwd", type=Path, default=PROJECT, help="install/render 的初始目录，默认项目根目录")
    args = parser.parse_args(argv)
    try:
        env_file = args.env_file.expanduser().resolve()
        cwd = args.cwd.expanduser().resolve()
        if args.action == "render":
            print(render_unit(PROJECT, env_file, cwd, args.user), end="")
            return
        if platform.system() != "Linux" or not shutil.which("systemctl"):
            parser.error("此脚本需要使用 systemd 的 Linux；可在其他系统使用 render 预览配置")
        if args.action in ("install", "start", "stop", "restart", "uninstall") and os.geteuid() != 0:
            parser.error("此操作需要 root 权限，请使用 sudo python3 scripts/deploy.py ...")
        if args.action == "install":
            install(PROJECT, env_file, cwd, args.user)
        elif args.action == "uninstall":
            if UNIT_PATH.exists():
                run("systemctl", "disable", "--now", UNIT_NAME)
                UNIT_PATH.unlink()
                run("systemctl", "daemon-reload")
            print("已卸载 qsh.service；源码、虚拟环境、凭证文件及历史日志均保留。")
        elif args.action == "logs":
            run("journalctl", "-u", UNIT_NAME, "-n", "100", "-f")
        elif args.action == "status":
            run("systemctl", "status", UNIT_NAME, "--no-pager")
        else:
            run("systemctl", args.action, UNIT_NAME)
    except (OSError, KeyError, ValueError) as error:
        parser.exit(1, f"部署失败：{error}\n")
    except subprocess.CalledProcessError as error:
        # Never repeat command arguments: they may contain sensitive paths.
        parser.exit(error.returncode, "操作失败，请检查上方输出。\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
