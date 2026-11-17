# WorkBuddy 多账号接入 Hermes

把一个 WorkBuddy 账号变成 Hermes 模型选择器里的一个 provider，一个账号一条独立链路。加新账号是一条命令，不是五处手工编辑。

实现：`tools/workbuddy-bridge/`（`wbctl.py` 加本机 bridge 运行时、回归测试、离线依赖包）。

## When to Use

- 用户新登录了一个 WorkBuddy 账号，要它出现在模型选择器里
- 某个账号积分用尽或被限流，要换到另一个账号
- 选择器里某个 WorkBuddy 分组整组失败，而另一个账号闲置
- 另一台 Hermes 要同样的多账号能力——把包拷过去 `init`，不要手抄五层

不适用：WorkBuddy 的登录、token 刷新、签到。这些由 WorkBuddy 桌面 App 维护，本方案只读它的凭据 vault。

## 目标

N 个账号在 Hermes 里是 N 个 provider，每个都能选中、能发消息、能被点名使用。加账号不改源码、不重建 app、不重启 gateway。

## 验收标准

每条都要能让 agent 自己判定通过，不靠感觉：

1. `wbctl.py audit` 报「全部通过」：每个账号 `/v1/models` 返回 200，每份活跃 config 都含该 slug，每份 profile 的 `.env` 都含 `key_env` 指向的键。
2. `python3 tests/test_wbctl.py` 11 项全过——它在隔离的 `HERMES_HOME` 里真写文件，不是 mock。
3. 新账号在桌面模型选择器里能选中，发一条消息拿到非空回复。
4. 每个账号 `/credits` 返回的 `account` 与注册表里的 `account` 一致：三个端口必须是三个不同账号，不是同一个账号被抄了三份。
5. 该账号的 `models:` 列表与它自己 `/v1/models` 返回的 id 集合一致（`· xN` 装饰后缀除外）。

## 可用资源

Python 3.9+（只用标准库）、Node 18+、WorkBuddy 桌面 App 已登录（凭据在 `~/.dsh/workbuddy-vault/`）。

```
python3 wbctl.py init                    # 全新机器：发现账号 + 部署运行时 + 写全部 config
python3 wbctl.py add [--slug X]          # 加一个号，端到端
python3 wbctl.py sync                    # 重拉各账号 /v1/models 并回写 config
python3 wbctl.py list                    # 账号、端口、模型数、健康
python3 wbctl.py audit                   # 只读体检，点名坏在哪一层
python3 wbctl.py remove --slug X
python3 wbctl.py --dry-run add           # 只打印，不写
```

全局开关 `--home` / `--vault` / `--dry-run`；`WBCTL_OFFLINE=1` 跳过所有 HTTP 探测，用于离线初始化和测试。

已配过号的老机器用 `init --force --regions <现有 regions.json>`：它会认领现有端口、显示名、模型清单和 vault 绑定，而不是按 vault 文件顺序重新猜。

## 已知陷阱

全部来自实际返工，按投入产出排序。

**注册表是唯一事实源。** `workbuddy-accounts.json` 派生端口、region、vault 绑定、模型清单和桌面端口表。为修一个号去改 `start-all.mjs` 里的死端口或前端 `CREDITS_BRIDGES` 表，就是五层漂移复发。改注册表，再 `sync`。

**每账号一把本地口令会串台。** 会话快照里的 `model_override` 只存 `model / provider / base_url`，不含凭据。picker 把 `base_url` 从 8767 切到 8768 时，请求仍带二号的 Bearer，正好撞上三号 bridge 的校验，报 `HTTP 401: invalid bridge token`。正解是一把 host-wide 口令，账号由该实例的 `shim-state.<region>.json` 加 vault 里的 `WB_ACCOUNT_ID` 决定；`bridge.mjs` 的 `authorized()` 接受本机任意 `token.*`。

**`key_env` 必须在每个 profile 的 `.env` 里都真实存在。** `profile_env_scope()` 让每个 bot 只读自己的 `.env`；只在全局 `.env` 有的键，对其他 bot 解析为空——切号不报错，日志仍显示走主号，这就是「切了没反应」最常见的形态。往各 profile 的 `.env` 补新凭据属于凭据落盘，需要单独授权，不能当顺手修复。

**换 token 之后 `git push` 报 `Invalid username or token`，不是 token 坏了。** macOS 默认 `credential.helper=osxkeychain`，它会用缓存里的旧凭据、绕过 `GIT_ASKPASS`；而 API 调用走 header、用的是新 token，于是出现「API 能开 PR、git 推不上去」的分裂。判断依据很干净：`POST /git/refs` 返回 201 说明 `Contents:write` 没问题，问题在认证层。推送时加 `-c credential.helper=` 清空 helper 才能保证用的是文件里那把。

**模型列表必须来自该账号自己的 `/v1/models`。** 账号之间目录不同：照抄主号会让选择器列出这个号没有的模型（实测三号没有 `hy3`、`hy3-x`、`hy4free`，独有 `hy4flash`、`hy4flash-free`），选中必然失败。装饰后缀 `· x0.29` 由 bridge 的 `stripDecor` 去掉，比对要用 wire id。

**provider 块必须是 `providers:` 的 2 空格子级。** 写成 4 空格会变成上一个 provider 的子键：YAML 解析通过、lint 通过、`hermes config get` 回「Config key not set」。验证要查路径而非语法——`yaml.safe_load` 之后走 `d['providers'].keys()`。

**每个 profile 都有自己的 `providers:` 段覆盖全局。** 只写当前 profile，其他 bot 看不到。

**枚举 config 要排除点开头的目录。** `profiles/.deleted/config.yaml` 这类历史残留会让只读体检报假问题，白烧一轮排查。

**`bridge.mjs` 不能单独启动。** 它启动时 `readFileSync` 自己的 `shim-state.<region>.json`，新 region 的 shim 还没跑过就必然 `ENOENT`。只有 supervisor 按顺序起 shim 再起 bridge 才行。

**桌面积分表是编译期常量。** Electron renderer 里的 `CREDITS_BRIDGES` 编进了 bundle，运行时改不动；要改源码、离线重建 asar、重新签名（改 asar 会破签名，必须重新 adhoc 签并 `codesign -v`）。这是可选步骤，不影响切模型和发消息，别为了一个显示数字去改 asar。

**`/credits` 有缓存。** 连续读会拿到同一个数字，别据此判断余额没变。报余额要带最近的刷新或到期时间，「0 积分」不写日期读起来像永久。

**加 region 之后 supervisor 必须重读清单。** 它只在启动时读账号列表，新 region 不会被拉起。旧版 supervisor 还写死端口，这时需要一次性换版——属于改生产运行时，先报备等放行。

## 输出规格

- 注册表 `$HERMES_HOME/workbuddy-accounts.json`，每个账号一条：`slug`、`region`、`vault`、`account`、`port`、`label`、`models`
- 端口表 `$HERMES_HOME/workbuddy-ports.json`：`slug → {port, region}`，桌面端积分表由它生成
- 每次写 config 前自动留底到 `$HERMES_HOME/backups/wbctl-<时间戳>-<操作>/`

## 验证

```bash
python3 tests/test_wbctl.py            # 11 项，隔离 HERMES_HOME 真写文件
python3 wbctl.py --dry-run add         # 只看会发生什么，不落盘
python3 wbctl.py audit                 # 装完做只读体检
```

外部状态必须读回：改完 config 用 `hermes -p <profile> config get providers` 读一遍，不是假设写成功；端到端真发一条消息，不是只看 `/v1/models` 返回 200。
