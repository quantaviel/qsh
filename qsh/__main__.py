import argparse
import logging
import os
from pathlib import Path

from aiohttp import web
from dotenv import dotenv_values

from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="qsh：QQ 机器人 shell（Webhook）")
    parser.add_argument("--appid", help="应用 ID，默认读取 QQ_APPID")
    parser.add_argument("--secret", help="应用密钥，默认读取 QQ_SECRET")
    parser.add_argument("--env-file", help="环境文件路径（相对启动目录或绝对路径），默认 .env")
    parser.add_argument("--cwd", default=".", help="初始工作目录，默认启动目录")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8080, help="HTTP 监听端口")
    args = parser.parse_args()
    try:
        env_file = Path(args.env_file).expanduser() if args.env_file is not None else Path(".env")
        if not env_file.is_absolute():
            env_file = Path.cwd() / env_file
        if args.env_file is not None or env_file.exists():
            with env_file.open(encoding="utf-8") as stream:
                config = dotenv_values(stream=stream)
        else:
            config = {}
    except (OSError, ValueError, RuntimeError):
        parser.error("无法读取环境文件，请检查 --env-file 路径、权限及 UTF-8 编码")
    if args.appid is None:
        args.appid = os.environ.get("QQ_APPID", config.get("QQ_APPID"))
    if args.secret is None:
        args.secret = os.environ.get("QQ_SECRET", config.get("QQ_SECRET"))
    if not args.appid or not args.appid.strip() or not args.secret or not args.secret.strip():
        parser.error("请通过 --appid/--secret、环境变量或环境文件（--env-file）提供 QQ_APPID 和 QQ_SECRET")
    if not 1 <= args.port <= 65535:
        parser.error("port 必须在 1–65535 之间")
    try:
        cwd = Path(args.cwd).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            parser.error("cwd 必须为目录")
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(f"无效 cwd：{error}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    web.run_app(create_app(args.appid, args.secret, cwd), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
