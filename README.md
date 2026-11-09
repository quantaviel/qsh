# qsh — QQ Shell

Python QQ 机器人，通过 QQ WebSocket 网关接收指令，通过当前官方 REST API 回复结果。支持 QQ 单聊和群内 @机器人；群事件兼容 `GROUP_AT_MESSAGE_CREATE` 与实际网关下发的 `GROUP_MESSAGE_CREATE`。

## 安装与启动

需要 Python 3.11+。本项目已创建 `.venv`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/qsh --appid APPID --secret SECRET [--cwd CWD]
```

也可以使用 `.venv/bin/python -m qsh --appid APPID --secret SECRET`。

推荐将凭证写入启动目录下的 `.env`，避免在命令行中传入 Secret：

```sh
cp .env.example .env
chmod 600 .env
# 编辑 .env，填写 QQ_APPID 和 QQ_SECRET
.venv/bin/qsh
```

```dotenv
QQ_APPID=your_appid
QQ_SECRET=your_app_secret
```

读取优先级为：命令行参数 > 进程环境变量 > 环境文件。默认读取**启动时当前目录**下的 `.env`；使用 `--env-file PATH` 可以指定其他文件，支持相对路径、绝对路径和 `~`。相对路径以启动目录为基准，不受 `--cwd` 影响，也不向父目录搜索。显式指定的文件不存在或无法读取时，启动会报错。`.env` 已加入 `.gitignore`；`.env.example` 仅包含占位符。

```sh
.venv/bin/qsh --env-file ./config/.env
.venv/bin/qsh --env-file /Users/citrus/Workspace/qsh/.env
```

| 参数 | 含义 |
| --- | --- |
| `--appid APPID` | QQ 开放平台应用 ID；未传时读取 `QQ_APPID` |
| `--secret SECRET` | 应用 AppSecret；未传时读取 `QQ_SECRET` |
| `--env-file PATH` | 环境文件路径，默认启动目录下的 `.env` |
| `--cwd CWD` | 初始目录，默认进程启动目录 |

`--cwd` **仅作为初始目录**，允许绝对路径、`..`、`~` 和符号链接，能够读取机器人进程有权限读取的任何普通文本文件。应在 QQ 后台配置好可使用机器人的测试用户与群。群内执行结果对群成员可见。命令行中的 Secret 应妥善保管。

## Linux systemd 服务部署

部署脚本为 [scripts/deploy.py](scripts/deploy.py)，需要使用 systemd 的 Linux（含 `systemctl`、`systemd-analyze`）、Python 3.11+（含 `venv`/`pip`）及 `util-linux` 提供的 `runuser`。将仓库放在服务用户能够读取、进入和写入的位置，以便创建和更新 `.venv`。例如，以准备运行机器人的普通用户登录服务器，在仓库目录执行：

```sh
cp .env.example .env
chmod 600 .env
# 编辑 .env，填写真实 QQ_APPID 和 QQ_SECRET
sudo python3 scripts/deploy.py install --user "$(id -un)"
```

`install` 会创建/复用项目的 `.venv`、以指定普通用户安装项目依赖并检查凭证文件，然后写入 `/etc/systemd/system/qsh.service`、启用开机启动并立即启动服务。服务不以 root 运行，凭证值不写入 unit 或启动参数。安装成功表示 systemd 接受了启动请求，QQ 登录是否成功以日志为准。安装前应停止原来的手动 qsh 进程，避免同一机器人重复连接。

可指定已有的其他普通用户、环境文件和机器人初始目录：

```sh
sudo python3 scripts/deploy.py install --user bot --env-file /srv/qsh/.env --cwd /srv/work
```

脚本不会创建用户或更改仓库归属；该用户必须能写入项目及虚拟环境、读取 `.env`、进入初始工作目录。参数中的相对路径以运行部署脚本时的目录为基准；未指定时，环境文件和初始目录默认使用项目根目录。保持仓库位置不变；移动后需重新安装服务。

```sh
sudo python3 scripts/deploy.py status
sudo python3 scripts/deploy.py logs      # 最近 100 行并持续跟随，Ctrl+C 退出
sudo python3 scripts/deploy.py stop
sudo python3 scripts/deploy.py start
sudo python3 scripts/deploy.py restart
sudo python3 scripts/deploy.py uninstall
```

服务登录系统前即可启动，退出后自动重启；日志交给 journald 管理。停止时向 qsh 发送 SIGTERM，程序停止网关连接并最多等待 20 秒处理队列，systemd 的总停止期限为 30 秒，最后清理服务控制组中的残余进程。`stop` 仅停止当前运行，开机启动仍保留；`uninstall` 停止并禁用服务、删除 unit，保留源码、虚拟环境、凭证与历史日志。

更新代码后重新运行 `install`，会更新依赖并重启；仅修改 `.env` 后使用 `restart` 即可。部署脚本不会自动拉取 Git 代码。在 macOS 等非 Linux 环境中，可仅预览配置而不安装：

```sh
python3 scripts/deploy.py render --user bot --env-file /srv/qsh/.env --cwd /srv/work
```

systemd 服务不会继承当前终端的 Tailscale/代理环境变量；服务器需要能直接连接 QQ HTTPS/WSS 服务。如需代理，另行配置服务出站网络。无需公网入站端口。

## QQ 后台配置

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人，取得 AppID 和 AppSecret，配置允许测试的单聊用户和 QQ 群。
2. 在后台选择/启用 WebSocket 事件接入方式；若之前配置了 Webhook，按后台提供的选项停用或切换该链路。不同应用能否使用 WebSocket，以平台实际开放能力和登录结果为准。
3. 按后台要求配置调用 API 的服务器公网出口 IP 白名单、消息发送及群聊/单聊事件权限。
4. 启动 `.venv/bin/qsh --env-file /你的路径/.env`。程序获取访问凭证，调用 `GET /gateway`，连接返回的 WSS 地址并通过 Identify 订阅群聊/单聊事件（`1 << 25`）。日志出现“QQ WebSocket 已就绪”表示登录成功。
5. 单聊发送 `ls`，或在群内发送 `@机器人 ls`。

本机只需出站访问 QQ HTTPS/WSS 服务，无需公网回调地址、HTTP 监听端口、内网穿透或 Tailscale Funnel。本版本已删除 Webhook 服务及 `--host`、`--port` 参数。现有 Funnel 不会由程序自动关闭；若该入口只用于 qsh，可自行停用。

## 命令

```text
ls
ls -a
ls /tmp
cat README.md
cat "文件名含空格.txt" another.txt
cd "子目录"
cd ..
cd /tmp
cd
pwd
grep -n "关键字" README.md
grep -ri "pattern" ./src
ps
ps aux
ls -la | grep README
cat README.md | grep -n "qsh"
ps aux | grep python
cd src && pwd
grep -q "pattern" file.txt && cat file.txt
grep -q "pattern" file.txt || cat fallback.txt
```

- `ls 参数…`、`cat 参数…`、`pwd 参数…`、`grep 参数…`、`ps 参数…`：直接执行本机系统程序，选项和输出遵循本机版本，例如 `ls -la`、`cat -n file.txt`、`grep -ri pattern ./src`、`ps aux`。
- `cd [目录]`：唯一的会话内建命令，改变当前用户在该会话中的目录；无参数进入家目录。`cd` 可通过 `&&`、`||` 与其他命令组合，但不能放入管道。
- `命令 | 命令 | …`：前一程序的标准输出直接连接后一程序的标准输入，支持不加空格的 `cat file|grep pattern`。最多连接 16 个命令，每一段均检查固定命令列表，检查全部通过后才开始执行。


- `A && B`：仅在 A 退出码为 0 时执行 B。
- `A || B`：仅在 A 退出码非 0 时执行 B。`grep` 无匹配的退出码为 1，会触发后续 `||`。

`|` 的优先级高于 `&&`、`||`；后两者优先级相同，从左到右求值。`A || B && C` 等价于先求值 `A || B`，成功后执行 C。被跳过的命令不启动、不改变目录，也不改变上一条退出状态。管道以最后一段的退出码为状态（未启用 pipefail）；例如 `cat missing | cat` 仍会显示前一段的错误，但最后的 `cat` 可以成功退出，因此会执行后面的 `&&`。

整条表达式在执行前检查语法和允许的命令，包括可能跳过的分支。程序未安装、启动失败或 `cd` 失败属于非零状态，可用 `||` 恢复。因超时或输出采集过量而主动终止时，停止剩余表达式，不执行恢复分支。一次请求最多 64 个命令。实际执行分支的输出按顺序合并，最终统一截断；跳过分支不产生输出。

参数使用 shell 风格引号解析。引号内或反斜杠转义的 `|` 是普通参数，例如 `grep -F "a|b" file.txt`。管道通过操作系统管道流式传输，中间结果不会先被回复长度限制截断；只捕获最后一段的标准输出和所有阶段的标准错误，错误信息不会流入后一阶段。最终合并为一条回复，截断至最多 1500 字符。

不调用系统 Shell，不支持重定向、`;`、括号分组、后台执行、通配符或变量/命令展开，也不能传入可执行文件路径绕过命令列表。第一段标准输入关闭；后续程序从管道读取。`grep` 返回 1 时按“无匹配”处理；其他失败阶段附带命令名与退出码，正常提前结束下游导致的上游 SIGPIPE 不报告为错误。

外部程序通过系统默认搜索路径解析，不使用用户的 `PATH` 或会话目录查找。子进程仅接收 `PATH`、`HOME` 及语言环境变量，不继承 QQ 凭证。整条组合命令（包含所有实际执行的管道）共享 5 秒执行期限，超时或达到输出采集上限时终止所有阶段的进程组。运行系统需提供这些程序；当前执行机制适用于 macOS/Linux。`cat` 采用系统行为，不再预先检查文本编码或文件类型，特殊文件阻塞受执行期限限制，非 UTF-8 输出以替换字符显示。

要允许其他固定程序，在 `qsh/programs.py` 的 `PROGRAM_NAMES` 中明确添加命令名称即可；该列表不能通过 QQ 消息更改。允许某个程序即允许其自身全部选项，添加支持脚本执行或外部命令调用的程序前，应先确定要开放的能力范围。

每个“聊天场景 + 会话 + 用户”独立维护目录；`cd` 不修改进程目录。重启会重置所有会话。每条命令回复一条文本，输出超过 1500 个字符会截断并标记，避免大文件产生大量消息。

## 实现与边界

- `qsh/shell.py`：管道语法解析及会话目录。
- `qsh/programs.py`：固定外部程序的解析、执行、超时及输出采集。
- `qsh/output.py`：共享回复长度限制。
- `qsh/qq.py`：`https://api.bot.qq.com` 上的凭证获取、缓存刷新、网关发现及单聊/群聊被动回复。
- `qsh/gateway.py`：WebSocket 登录、心跳 ACK、会话恢复、自动重连及生命周期。
- `qsh/runtime.py`：事件解析、内存去重、有界消息队列与后台命令处理。
- `qsh/__main__.py`：启动参数与异步程序入口。

消息事件入队后更新网关序列号，文件操作和外部程序在线程中执行，不阻塞心跳。使用一个后台消费者按接收顺序执行命令和回复，适合小规模个人使用。队列最多 256 条，满时断开并尝试通过 Resume 补发；尚未入队的事件不更新序列号，也不计入去重记录。

按网关 Hello 指定周期发送心跳，下一周期未收到 ACK 时重连；断线/网关要求重连时带 session_id 与已接收序列号恢复会话。遇到 Invalid Session 清除恢复状态并重新鉴权。连接失败以 1–30 秒退避重试，成功登录后重置。REST 请求和网关登录均有超时，退出时最多等待 20 秒处理已入队消息。进程重启不恢复网关会话。

接收去重记录保留至多两小时、10000 条，目录、队列与去重均仅在内存中。不保证进程崩溃后的消息恢复；发送失败记录错误而不自动重发，避免不确定的发送结果产生重复消息。未实现持久化、额外用户授权系统、频道消息或任意程序执行。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

测试包含系统命令、目录隔离、多级管道、逻辑短路及退出状态、目录切换后组合执行、引号和转义、程序环境隔离、超时清理、输出上限、WebSocket Identify/Resume、心跳 ACK、无效会话、补发去重、队列背压、消息路由、REST 凭证缓存以及 HTTP 200 业务错误。协议测试会监听本机临时端口，不访问真实 QQ 服务。

## API 核对依据

2026-11-08 核对当前官方文档后直接实现协议，未依赖旧 `botpy` SDK。REST 与网关发现统一使用 `https://api.bot.qq.com`，WebSocket 地址使用 `GET /gateway` 的返回值，不硬编码旧域名。

- [获取访问凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)：AppID/ClientSecret、有效期及 HTTP 200 中的业务错误。
- [API 调用指南](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)：统一域名、QQBot 鉴权与 err_code。
- [事件订阅与通知](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)：WebSocket Hello、Identify、Heartbeat、Resume 与群聊/单聊 intents。
- [获取 WSS 接入点](https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html)。
- [发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)、[发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)。
- [单聊事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)、[群 @事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html)。

本地模拟协议测试不代表当前 AppID 的真实 QQ 消息收发已完成；实际使用需确认平台为该应用开放 WebSocket 接入和相应权限。

## 群聊交互排查

服务日志会记录网关事件类型、消息入队场景和回复成功状态，不记录消息正文、用户/群标识或凭证。群聊无回复时，先查看 `journalctl -u qsh.service`：没有群事件时检查平台群聊接入配置；有事件但未入队时检查事件路由或解析警告；已入队但回复失败时检查 QQ API 错误码。`GROUP_MESSAGE_CREATE` 与 `GROUP_AT_MESSAGE_CREATE` 共用群会话和去重逻辑，避免同一消息被重复执行。
