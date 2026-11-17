# 桌面端积分 pill（可选，只影响显示）

`workbuddy-credits-pill.tsx` 里有一张写死的 `CREDITS_BRIDGES` 表，决定底部积分
读数去查哪个本地端口。插件的核心功能（切模型、发消息）不依赖它，**这张表
只影响积分显示**。

## 有源码树的机器（本机）

```bash
python3 desktop/sync_credits_pill.py \
  --home ~/.hermes \
  --pill ~/.hermes/hermes-agent-v0.22.5-backup/apps/desktop/src/app/chat/composer/workbuddy-credits-pill.tsx
```

脚本从 `~/.hermes/workbuddy-ports.json` 读账号表，生成常量块替换源码，并留底。
之后跑 `desktop/rebuild-asar.mjs` 离线重建 `app.asar`、重新 adhoc 签名、
`codesign -v` 验证，退出并重开 Desktop 生效。

`wbctl add` 会同步更新 `workbuddy-ports.json`，所以加第五、第六个号时不用再
手工改源码，只需重跑这两步。

## 只有打包好的 app（没有源码树）

跳过。模型切换和发消息完全正常，只是积分 chip 不显示新号。别为了一个显示
去改 app.asar。

## 为什么不放进主流程

Electron renderer 里那张表是编译进 bundle 的常量，运行时改不了；重建 asar
需要源码树、`@electron/asar` 和 codesign，只有本机的开发目录具备。把它做成
独立可选步骤，插件在别的 Hermes 上才不会被这一步卡住。
