# workbuddy-bridge

把 WorkBuddy 多账号接进 Hermes 的可复用插件。

解决的问题：原来加一个 WorkBuddy 号要手工改五处（`regions.json`、
`start-all.mjs` 端口、`token.*`、8 份 `config.yaml` 的 provider 段、桌面端
`CREDITS_BRIDGES` 表），漏一处就出 `HTTP 401: invalid bridge token`，排查
要一层层反查。这个插件把五处收成一份账号清单 `workbuddy-accounts.json`
和一条命令。

## 两条要求怎么落

**1. 以后加新号直接加进来用**

```bash
python3 wbctl.py add                      # 自动挑 vault 里下一个未用账号、分配下一个端口
python3 wbctl.py add --slug workbuddy-4  # 或者自己指定名字
```

`add` 一次做完：发现账号 → 分配 region/端口 → 起本地 shim → 从运行中的
bridge 拉真实模型目录 → 写 8 份 config（全局 + 每个 profile）→ 更新
`workbuddy-ports.json` → 交叉验证新号能发请求。加第五、第六个号是同一条
命令，不再改源码、不重建 app.asar、不重启 gateway。

**2. 别的 Hermes 直接用**

整个包自包含：运行时 4 个 `.mjs`/`package.json`、`vendor/` 里带上
`dsh-connect-workbuddy` 的 tgz（离线装，不依赖 npm 联网）、`wbctl.py` 只用
Python 标准库。拷到另一台机器，跑一次 `init`，它自己发现本机 WorkBuddy
登录态、生成账号清单、部署 bridge、写 config。

## 前置条件

- Python 3.9+（只用标准库）
- Node 18+
- 本机 WorkBuddy 桌面 App 已登录，凭据库在 `~/.dsh/workbuddy-vault/`
- 一个 `HERMES_HOME`（默认 `~/.hermes`）

## 别的 Hermes 上安装

```bash
tar -xzf workbuddy-bridge-1.0.0.tgz
cd workbuddy-bridge
python3 wbctl.py init
python3 wbctl.py audit
```

`init` 做这些事，每一步都可回退：
- 扫 `~/.dsh/workbuddy-vault/{cn,global}/*.json` 发现账号
- 分配 slug（`workbuddy`、`workbuddy-global`、`workbuddy-2`…）与端口（8765 起）
- 部署 `runtime/` 到 `$HERMES_HOME/workbuddy-bridge/`，从 `vendor/` 离线装依赖
- 把 `WB_SHIM_TOKEN` 幂等写进全局 `.env` 和每个 profile 的 `.env`
- 给全局 config + 每个 profile config 写 provider 段
- 生成 `workbuddy-accounts.json`、`workbuddy-ports.json`、`regions.json`
- 起 supervisor（用 `--no-start` 可只部署不起进程）

改 config 和 `.env` 前自动留底到 `$HERMES_HOME/backups/wbctl-<时间戳>-sync/`。

## 接管现有的 WorkBuddy 配置

机器上已经手工配过号时，别让 init 重新猜：

```bash
python3 wbctl.py init --force --regions ~/workbuddy-hermes/regions.json
```

`--regions` 指向现有 `regions.json`，`init` 会认领现有 config 里的端口、显示名、
模型清单和 vault 账号绑定，逐项对齐，不清空已有模型列表。本机 4 个号、
8 份 config 已用真实数据跑过，四个 provider 的端口/名称/模型清单与生产配置
逐项一致。

## 命令

| 命令 | 作用 |
| --- | --- |
| `wbctl.py init` | 发现账号、部署运行时、写 config（全新机器一次到位） |
| `wbctl.py add` | 加一个新号，一条命令串起全部步骤 |
| `wbctl.py sync` | 从运行中的 bridge 重拉模型目录并回写 8 份 config |
| `wbctl.py list` | 列账号、端口、模型数、健康状态 |
| `wbctl.py audit` | 只读体检：口令、8 份 config、bridge 可达性 |
| `wbctl.py remove --slug X` | 摘掉一个号 |

全局开关：`--home`（HERMES_HOME）、`--vault`（凭据库）、`--dry-run`（只打印不写）。
`WBCTL_OFFLINE=1` 跳过所有 HTTP 探测，用于离线初始化和自动化测试。

## 凭据模型

参照桌面插件「每个账号一套独立凭据栈、不回落默认」的思路：

- **一把本机 front-door 口令** `WB_SHIM_TOKEN`，写在每个 profile 自己的
  `.env`（`profile_env_scope()` 让各 bot 只读自己的 `.env`，所以必须每份都有）
- **账号由 shim 自己决定**：该实例读 `shim-state.<region>.json` 与 vault 里
  绑定的凭据，不按 front-door 口令区分账号
- `bridge.mjs` 的 `authorized()` 接受本机任意 `token.*` 本地口令，避免旧会话
  切了 `base_url` 却还带上一把口令而撞 401
- 上游凭据始终不出本机，值不写进 config、不进日志、不进包

这同时修掉了两个历史坑：`key_env` 指向 profile `.env` 里不存在的 key，以及
切换时端口换了、本地口令没换。

## 包内文件

```
plugin.yaml                  Hermes 插件清单（加载时只读，不写生产配置）
__init__.py                  插件壳：initialize() 只数账号
wbctl.py                     核心 CLI，标准库
runtime/                     bridge.mjs / shim.mjs / start-all.mjs / package.json
vendor/                      dsh-connect-workbuddy tgz + SHA256SUMS
desktop/sync_credits_pill.py 可选：从 ports.json 重生成桌面端积分表
desktop/rebuild-asar.mjs     可选：离线重建 app.asar
desktop/README.md            桌面端积分显示的边界说明
tests/test_wbctl.py          10 项真写文件的回归测试
```

`__init__.py` 刻意保持 inert：装插件和改生产配置是两件事，只有操作者显式跑
`init|add|sync` 才动 config、`.env` 和桌面端。

## 验证

```bash
python3 tests/test_wbctl.py        # 10 项，隔离 HERMES_HOME 真写文件
python3 wbctl.py --dry-run add     # 只看会发生什么
python3 wbctl.py audit             # 装完做只读体检
```

## 本机现有环境的一次性接管

本机当前的 supervisor 在 `~/workbuddy-hermes/`，是写死 `REGIONS/PORTS` 的旧版，
读自己目录里的 `regions.json`。插件部署目录是 `$HERMES_HOME/workbuddy-bridge/`，
用的是清单驱动版 `start-all.mjs`。两者不是同一个目录。

`wbctl add` 已经把新号写进 `$HERMES_HOME/workbuddy-accounts.json`、
`$HERMES_HOME/workbuddy-ports.json`、8 份 config，以及
`$HERMES_HOME/workbuddy-bridge/` 下的 `regions.json` / `token.<region>`。但要让
新号真的监听新端口，需要把旧 supervisor 换成清单驱动版并重启一次：

```bash
# 1) 留底后换版（只换一个文件）
cp ~/workbuddy-hermes/start-all.mjs ~/.hermes/backups/start-all-pre-wbctl.mjs
cp ~/Desktop/workbuddy-bridge/runtime/start-all.mjs ~/workbuddy-hermes/start-all.mjs

# 2) 重启 supervisor（子进程 3 秒自动拉起，shim/vault 不动）
pkill -f 'node .*start-all.mjs'
WB_HOME=~/.hermes nohup node ~/workbuddy-hermes/start-all.mjs \
  >> ~/.hermes/logs/wb-supervisor.log 2>&1 &

# 3) 拉真实模型、验收
python3 ~/Desktop/workbuddy-bridge/wbctl.py sync
python3 ~/Desktop/workbuddy-bridge/wbctl.py audit
```

`WB_HOME` 让旧目录里的清单驱动 supervisor 去读 `~/.hermes/workbuddy-accounts.json`。
这次交付**没有执行这三步**：它替换生产运行时文件、重启本地 bridge 进程，属于动
生产，等鹏哥点头再做。做完之后，以后加号就只剩 `wbctl add` + `wbctl sync` 两条。

别的 Hermes 全新安装没有这个历史包袱，`init` 直接就是清单驱动 supervisor。

## 边界

- 桌面端底部积分 chip 的 `CREDITS_BRIDGES` 是编译进 Electron bundle 的常量，
  运行时改不了。加新号后想让积分显示跟上，见 `desktop/README.md`；**不影
  响切模型和发消息**。
- 上游账号的登录态由 WorkBuddy 桌面 App 维护，插件只读 vault，不负责登录、
  刷新 token 或签到。
- `init` 不碰 `~/.dsh/` 以外的系统配置，不起非 loopback 监听。
