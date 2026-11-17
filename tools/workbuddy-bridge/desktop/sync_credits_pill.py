#!/usr/bin/env python3
"""Regenerate the desktop CREDITS_BRIDGES table from workbuddy-ports.json.

Only needed on a machine that has the Hermes desktop SOURCE tree. Run this
after `wbctl add`, then rebuild app.asar with rebuild-asar.mjs. On a machine
with only the packaged app (no source tree), skip it: model switching and
chat work without it, only the credits chip stays stale.
"""
import argparse
import json
import os
import re
import shutil
import datetime

START = re.compile(r"const CREDITS_BRIDGES[^=]*= \{", re.M)


def build_block(ports):
    lines = ["const CREDITS_BRIDGES: Record<string, { port: number; region: string }> = {"]
    for slug, v in sorted(ports.items()):
        key = slug if re.match(r"^[A-Za-z_$][\w$]*$", slug) else "'" + slug + "'"
        lines.append("  %s: { port: %d, region: '%s' }," % (key, v["port"], v["region"]))
    lines.append("}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME") or
                    os.path.join(os.path.expanduser("~"), ".hermes"))
    ap.add_argument("--pill", required=True, help="workbuddy-credits-pill.tsx 路径")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ports = json.load(open(os.path.join(a.home, "workbuddy-ports.json")))
    txt = open(a.pill).read()
    m = START.search(txt)
    if not m:
        raise SystemExit("找不到 CREDITS_BRIDGES 定义: " + a.pill)
    close = txt.find("}", m.end())
    if close < 0:
        raise SystemExit("找不到 CREDITS_BRIDGES 结尾")
    new = txt[:m.start()] + build_block(ports) + txt[close + 1:]
    print("新表:", len(ports), "个账号")
    print(build_block(ports))
    if a.dry_run or new == txt:
        print("(dry-run 或无变化)" if a.dry_run else "无变化")
        return
    b = a.pill + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(a.pill, b)
    open(a.pill, "w").write(new)
    print("已写入:", a.pill, " 留底:", b)
    print("下一步: 用 desktop/rebuild-asar.mjs 离线重建 app.asar 并重签")


if __name__ == "__main__":
    main()
