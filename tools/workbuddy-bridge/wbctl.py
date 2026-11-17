#!/usr/bin/env python3
"""wbctl - add a WorkBuddy account to Hermes in one command.

Portable: stdlib only. Runs on any machine with Python 3.9+, Node 18+,
a WorkBuddy desktop login (the credential vault), and a Hermes home.

The design follows dsh-connect-workbuddy: one local shim per account, but
the front-door password is a single host-wide token. The account is chosen
by the shim's own vault-pinned credential, never by the password. That is
what makes adding a fourth/fifth account a registry edit instead of a
config/source rebuild, and what stops the "picked account A, sent B's key"
401 that a restored session used to produce.
"""
import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_SRC = os.path.join(PLUGIN_DIR, "runtime")
DEFAULT_HOME = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
REGISTRY_NAME = "workbuddy-accounts.json"
RUNTIME_NAME = "workbuddy-bridge"

# One host-wide loopback password. Accounts are separated by the vault pin in
# shim-state.<region>.json, NOT by which bearer the client happens to hold.
KEY_ENV = "WB_SHIM_TOKEN"


# ---------------------------------------------------------------- paths
def config_files(home):
    out = [os.path.join(home, "config.yaml")]
    pd = os.path.join(home, "profiles")
    if os.path.isdir(pd):
        out += sorted(
            os.path.join(pd, p, "config.yaml")
            for p in os.listdir(pd)
            if not p.startswith(".")          # .deleted / .bak / 隐藏残留不算活跃 profile
            and os.path.isfile(os.path.join(pd, p, "config.yaml"))
        )
    return [p for p in out if os.path.isfile(p)]


def env_files(home):
    out = [os.path.join(home, ".env")]
    pd = os.path.join(home, "profiles")
    if os.path.isdir(pd):
        out += sorted(
            os.path.join(pd, p, ".env")
            for p in os.listdir(pd)
            if not p.startswith(".")
            and os.path.isfile(os.path.join(pd, p, ".env"))
        )
    return out


def registry_path(home):
    return os.path.join(home, REGISTRY_NAME)


def runtime_dir(home):
    return os.path.join(home, RUNTIME_NAME)


def vault_root(args):
    return args.vault or os.path.join(os.path.expanduser("~"), ".dsh", "workbuddy-vault")


def load_registry(home):
    p = registry_path(home)
    if not os.path.isfile(p):
        return {"version": 1, "accounts": []}
    with open(p) as f:
        return json.load(f)


def save_registry(home, reg, dry):
    p = registry_path(home)
    txt = json.dumps(reg, ensure_ascii=False, indent=2) + "\n"
    if dry:
        return p
    with open(p, "w") as f:
        f.write(txt)
    return p


# ---------------------------------------------------------------- backup
def backup(files, home, tag, dry):
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    b = os.path.join(home, "backups", "wbctl-%s-%s" % (ts, tag))
    if dry:
        return b
    os.makedirs(b, exist_ok=True)
    for f in files:
        if not os.path.isfile(f):
            continue
        rel = os.path.relpath(f, home).replace("/", "__")
        shutil.copy2(f, os.path.join(b, rel))
    return b


# ---------------------------------------------------------------- yaml
def yaml_scalar(s):
    s = str(s)
    if (s[:1] in "-?:!&*#{}[]|>@`\"'" or ": " in s or s.strip() != s
            or s.lower() in ("null", "true", "false", "yes", "no")):
        return "'" + s.replace("'", "''") + "'"
    return s


def provider_block(slug, name, port, models):
    lines = [
        "  %s:" % slug,
        "    name: %s" % yaml_scalar(name),
        "    api: http://127.0.0.1:%d/v1" % port,
        "    key_env: %s" % KEY_ENV,
        "    transport: openai",
        "    discover_models: false",
        "    models:",
    ]
    for m in models:
        lines.append("      - %s" % yaml_scalar(m))
    return "\n".join(lines) + "\n"


def upsert_provider(text, slug, block):
    """Replace/insert one provider in the top-level `providers:` mapping.

    Text-level on purpose: PyYAML/ruamel is not guaranteed on another Hermes,
    and a round-trip would rewrite the whole file. Two-space indent is
    mandatory (four spaces nests the entry inside the previous provider).
    """
    m = re.search(r"^providers:\s*$", text, re.M)
    if not m:
        return text.rstrip("\n") + "\n\nproviders:\n" + block
    start = m.end() + 1
    nxt = re.search(r"^\S", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    seg = text[start:end]
    mm = re.search(r"^  " + re.escape(slug) + r":\s*$", seg, re.M)
    if mm:
        m2 = re.search(r"^  \S", seg[mm.end():], re.M)
        be = mm.end() + m2.start() if m2 else len(seg)
        seg = seg[:mm.start()] + block + seg[be:]
    else:
        if not seg.endswith("\n"):
            seg += "\n"
        seg += block
    return text[:start] + seg + text[end:]


def remove_provider(text, slug):
    m = re.search(r"^providers:\s*$", text, re.M)
    if not m:
        return text
    start = m.end() + 1
    nxt = re.search(r"^\S", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    seg = text[start:end]
    mm = re.search(r"^  " + re.escape(slug) + r":\s*$", seg, re.M)
    if not mm:
        return text
    m2 = re.search(r"^  \S", seg[mm.end():], re.M)
    be = mm.end() + m2.start() if m2 else len(seg)
    return text[:start] + seg[:mm.start()] + seg[be:] + text[end:]


# ---------------------------------------------------------------- vault
def discover_vault(vault):
    """List vaulted accounts. Never reads or prints token material."""
    out = []
    for region in ("cn", "global"):
        d = os.path.join(vault, region)
        if not os.path.isdir(d):
            continue
        for fn in sorted(f for f in os.listdir(d) if f.endswith(".json")):
            try:
                with open(os.path.join(d, fn)) as f:
                    doc = json.load(f)
            except (ValueError, OSError):
                continue
            acct = str(doc.get("accountId") or fn[:-5])
            nick = doc.get("nickname") or doc.get("name") or doc.get("account") or ""
            if isinstance(nick, dict):
                nick = nick.get("name") or nick.get("uid") or ""
            nick = str(nick)
            out.append({"vault": region, "account": acct, "nick": nick, "file": fn})
    return out


def next_slot(reg, vault):
    """Pick the next unused vault account, or the next free region/port."""
    used = {a["account"] for a in reg["accounts"]}
    cn_seen = sum(1 for a in reg["accounts"] if a["vault"] == "cn")
    used_ports = {a["port"] for a in reg["accounts"]}
    return {
        "cn_seen": cn_seen,
        "used": used,
        "used_ports": used_ports,
        "vault_accounts": discover_vault(vault),
    }


def allocate(reg, vault, slug=None, account=None, port=None, name=None):
    """Build the account entry for a new (Nth domestic) account."""
    cn_seen = sum(1 for a in reg["accounts"] if a["vault"] == "cn")
    n = cn_seen + 1  # 1st cn is `workbuddy`, 2nd is `workbuddy-2`, ...
    if n == 1:
        region, defslug, defport, label = "cn", "workbuddy", 8765, "CN"
    else:
        region, defslug, defport, label = "cn%d" % n, "workbuddy-%d" % n, 8765 + n, "CN%d" % n
    slug = slug or defslug
    port = port or defport
    used_ports = {a["port"] for a in reg["accounts"]}
    while port in used_ports:
        port += 1

    used = {a["account"] for a in reg["accounts"]}
    picked = None
    if account:
        for v in discover_vault(vault):
            if v["account"].lower().startswith(account.lower()) or account.lower() in v["account"].lower():
                picked = v
                break
        if picked is None:
            raise SystemExit("账号 %s 不在 vault；先在 WorkBuddy 客户端登录该账号" % account)
    else:
        for v in discover_vault(vault):
            if v["vault"] == "cn" and v["account"] not in used:
                picked = v
                break
        if picked is None:
            raise SystemExit("vault 里没有未注册的国内账号；先在 WorkBuddy 客户端登录新账号")
    return {
        "slug": slug,
        "name": name or ("WorkBuddy %s号" % n if n > 1 else "WorkBuddy"),
        "region": region,
        "vault": picked["vault"],
        "account": picked["account"],
        "port": port,
        "label": label,
        "nick": picked.get("nick", ""),
    }


# ---------------------------------------------------------------- runtime
def http_json(url, token=None, payload=None, timeout=8):
    if os.environ.get("WBCTL_OFFLINE") == "1":
        return 0, {}
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def read_env_key(path, key):
    if not os.path.isfile(path):
        return ""
    m = re.search(r"^" + re.escape(key) + r"=(.*)$", open(path).read(), re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def ensure_env_key(path, value, dry):
    """Idempotently set one key. Returns True when it wrote."""
    txt = open(path).read() if os.path.isfile(path) else ""
    if re.search(r"^" + re.escape(KEY_ENV) + r"=", txt, re.M):
        return False
    if dry:
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        if txt and not txt.endswith("\n"):
            f.write("\n")
        f.write("%s=%s\n" % (KEY_ENV, value))
    os.chmod(path, 0o600)
    return True


def deploy_runtime(home, dry):
    dst = runtime_dir(home)
    if dry:
        return dst
    os.makedirs(dst, exist_ok=True)
    for fn in ("bridge.mjs", "shim.mjs", "start-all.mjs", "package.json", "regions.json"):
        src = os.path.join(RUNTIME_SRC, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst, fn))
    # vendored tgz so another machine needs no DSH install or network
    vend = os.path.join(PLUGIN_DIR, "vendor")
    tgzs = [f for f in os.listdir(vend) if f.endswith(".tgz")] if os.path.isdir(vend) else []
    if tgzs and not os.path.isdir(os.path.join(dst, "node_modules", "dsh-connect-workbuddy")):
        subprocess.run(["npm", "install", "--no-save", "--no-audit", "--no-fund",
                        os.path.join(vend, sorted(tgzs)[-1])],
                       cwd=dst, capture_output=True, timeout=300)
    return dst


def write_regions(rd, reg, dry):
    """Legacy regions.json stays in sync for the unmodified upstream supervisor."""
    obj = {a["region"]: {"vault": a["vault"], "account": a["account"]} for a in reg["accounts"]}
    txt = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    p = os.path.join(rd, "regions.json")
    if not dry:
        with open(p, "w") as f:
            f.write(txt)
    return p


def write_tokens(rd, reg, value, dry):
    """One host-wide password, one file per region (bridge reads its own)."""
    for a in reg["accounts"]:
        p = os.path.join(rd, "token." + a["region"])
        if not dry:
            with open(p, "w") as f:
                f.write(value)
            os.chmod(p, 0o600)


def write_ports_file(home, reg, dry):
    """What a one-time desktop patch reads so new accounts show credits too."""
    obj = {a["slug"]: {"port": a["port"], "region": a["label"]} for a in reg["accounts"]}
    p = os.path.join(home, "workbuddy-ports.json")
    if not dry:
        with open(p, "w") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return p


def parse_existing_providers(text):
    """Read slug -> {port,name,models} from an existing config."""
    m = re.search(r"^providers:\s*$", text, re.M)
    if not m:
        return {}
    start = m.end() + 1
    nxt = re.search(r"^\S", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    seg = text[start:end]
    heads = list(re.finditer(r"^  (workbuddy[\w-]*):\s*$", seg, re.M))
    out = {}
    for i, h in enumerate(heads):
        be = heads[i + 1].start() if i + 1 < len(heads) else len(seg)
        block = seg[h.end():be]
        pm = re.search(r"api: http://127\.0\.0\.1:(\d+)/v1", block)
        nm = re.search(r"^\s+name:\s*(.+?)\s*$", block, re.M)
        models = [s.strip().strip("\"'") for s in
                  re.findall(r"^\s+- (.+?)\s*$", block, re.M)]
        out[h.group(1)] = {
            "port": int(pm.group(1)) if pm else None,
            "name": (nm.group(1).strip("\"'") if nm else h.group(1)),
            "models": models,
        }
    return out


def adopt_config(reg, cfgs, regions_path):
    """On an install that already has WorkBuddy providers, keep its ports,
    names, model lists and vault pins instead of guessing from vault order."""
    existing = parse_existing_providers(open(cfgs[0]).read()) if cfgs else {}
    rmap = {}
    if regions_path and os.path.isfile(regions_path):
        try:
            rmap = json.load(open(regions_path))
        except (ValueError, OSError):
            pass
    for x in reg["accounts"]:
        e = existing.get(x["slug"])
        if e:
            if e["port"]:
                x["port"] = e["port"]
            x["name"] = e["name"]
            if e["models"]:
                x["models"] = e["models"]
        r = rmap.get(x["region"])
        if r:
            x["vault"] = r.get("vault", x["vault"])
            if r.get("account"):
                x["account"] = r["account"]
    # a provider in config but not in the freshly discovered registry
    known = {x["slug"] for x in reg["accounts"]}
    for slug, e in existing.items():
        if slug in known or not e["port"]:
            continue
        region = "global" if slug.endswith("global") else "cn"
        r = rmap.get(region, {})
        reg["accounts"].append({
            "slug": slug, "name": e["name"], "region": region,
            "vault": r.get("vault", region), "account": r.get("account", ""),
            "port": e["port"], "label": "GL" if region == "global" else "CN",
            "models": e["models"], "nick": "",
        })
    reg["accounts"].sort(key=lambda x: x["port"])


def parse_existing_models(text, slug):
    """Read the model list already in a config, so init never blanks it."""
    m = re.search(r"^providers:\s*$", text, re.M)
    if not m:
        return []
    start = m.end() + 1
    nxt = re.search(r"^\S", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    seg = text[start:end]
    mm = re.search(r"^  " + re.escape(slug) + r":\s*$", seg, re.M)
    if not mm:
        return []
    m2 = re.search(r"^  \S", seg[mm.end():], re.M)
    be = mm.end() + m2.start() if m2 else len(seg)
    found = re.findall(r"^\s+- (.+?)\s*$", seg[mm.start():be], re.M)
    return [s.strip().strip("\"'") for s in found]


def hydrate_models(reg, cfgs, token):
    """Prefer the live catalog; otherwise keep whatever the config already has."""
    for x in reg["accounts"]:
        code, models = http_json("http://127.0.0.1:%d/v1/models" % x["port"], token, timeout=4)
        if code == 200 and models.get("data"):
            x["models"] = [m["id"] for m in models["data"]]
        if not x.get("models") and cfgs:
            kept = parse_existing_models(open(cfgs[0]).read(), x["slug"])
            if kept:
                x["models"] = kept


def sync_configs(home, reg, cfgs, dry):
    b = backup(cfgs, home, "sync", dry)
    for c in cfgs:
        txt = open(c).read()
        for a in reg["accounts"]:
            txt = upsert_provider(txt, a["slug"], provider_block(
                a["slug"], a["name"], a["port"], a.get("models") or []))
        if not dry:
            with open(c, "w") as f:
                f.write(txt)
    return b


def prune_configs(home, slug, cfgs, dry):
    b = backup(cfgs, home, "remove", dry)
    for c in cfgs:
        txt = open(c).read()
        new = remove_provider(txt, slug)
        if new != txt and not dry:
            with open(c, "w") as f:
                f.write(new)
    return b


# ---------------------------------------------------------------- commands
def cmd_init(a):
    home = a.home
    cfgs = config_files(home)
    print("HERMES_HOME :", home)
    print("config 份数 :", len(cfgs))
    print("vault       :", vault_root(a))
    vault = vault_root(a)
    found = discover_vault(vault)
    print("vault 账号  :", len(found), "个（只列账号 id 前 8 位）")
    for v in found:
        print("   ", v["vault"], v["account"][:8], v["nick"][:20])
    if not found:
        print("  提示：先在 WorkBuddy 客户端登录账号，凭据会写进 vault")

    reg = load_registry(home)
    if reg["accounts"] and not a.force:
        print("已有 registry（%d 个账号），用 --force 重建" % len(reg["accounts"]))
    else:
        reg = {"version": 1, "accounts": []}
        for v in found:
            if v["vault"] == "global":
                reg["accounts"].append({
                    "slug": "workbuddy-global", "name": "WorkBuddy 国际版",
                    "region": "global", "vault": "global", "account": v["account"],
                    "port": 8766, "label": "GL", "nick": v.get("nick", ""), "models": []})
        cn = [v for v in found if v["vault"] == "cn"]
        for i, v in enumerate(cn, 1):
            if i == 1:
                slug, region, port, label, name = "workbuddy", "cn", 8765, "CN", "WorkBuddy"
            else:
                slug, region, port, label, name = ("workbuddy-%d" % i, "cn%d" % i,
                                                   8765 + i, "CN%d" % i, "WorkBuddy %s号" % i)
            reg["accounts"].append({
                "slug": slug, "name": name, "region": region, "vault": "cn",
                "account": v["account"], "port": port, "label": label,
                "nick": v.get("nick", ""), "models": []})
        reg["accounts"].sort(key=lambda x: x["port"])

    token = read_env_key(os.path.join(home, ".env"), KEY_ENV)
    if not token:
        token = "wb-" + secrets.token_urlsafe(32)
    envs = env_files(home)
    wrote = [p for p in envs if ensure_env_key(p, token, a.dry)]
    adopt_config(reg, cfgs, a.regions)
    hydrate_models(reg, cfgs, token)
    rd = deploy_runtime(home, a.dry)
    save_registry(home, reg, a.dry)
    write_regions(rd, reg, a.dry)
    write_tokens(rd, reg, token, a.dry)
    write_ports_file(home, reg, a.dry)
    b = sync_configs(home, reg, cfgs, a.dry)
    print("\n部署目录   :", rd)
    print("registry   :", registry_path(home))
    print("账号数     :", len(reg["accounts"]))
    print(".env 需补  :", len(wrote), "份（%s 幂等，不输出值）" % KEY_ENV)
    print("留底       :", b, "(dry-run)" if a.dry else "")
    if not a.dry and not a.no_start:
        print("启动       : 运行 node %s/start-all.mjs" % rd)
    print("完成。用 wbctl audit 做只读体检。")


def cmd_add(a):
    home = a.home
    vault = vault_root(a)
    reg = load_registry(home)
    if any(x["slug"] == (a.slug or "") for x in reg["accounts"]):
        raise SystemExit("slug %s 已存在；用 wbctl sync 修配置或换一个 slug" % a.slug)
    entry = allocate(reg, vault, a.slug, a.account, a.port, a.name)
    print("新账号     :", entry["slug"], entry["region"], "port", entry["port"],
          "account", entry["account"][:8], "vault", entry["vault"])

    # pull the live catalog from the new bridge when it is already up
    token = read_env_key(os.path.join(home, ".env"), KEY_ENV)
    code, models = http_json("http://127.0.0.1:%d/v1/models" % entry["port"], token)
    if code == 200 and models.get("data"):
        entry["models"] = [m["id"] for m in models["data"]]
        print("模型目录   : 从 %d 实时拉到 %d 个" % (entry["port"], len(entry["models"])))
    else:
        entry["models"] = list((reg["accounts"][-1].get("models") or []) if reg["accounts"] else [])
        print("模型目录   : bridge 未起(%s)，先沿用 %d 个占位；起实例后跑 wbctl sync 刷新"
              % (code or "ERR", len(entry["models"])))

    reg["accounts"].append(entry)
    reg["accounts"].sort(key=lambda x: x["port"])
    cfgs = config_files(home)
    b = backup(cfgs + [registry_path(home)], home, "add-" + entry["slug"], a.dry)
    save_registry(home, reg, a.dry)
    rd = runtime_dir(home)
    write_regions(rd, reg, a.dry)
    write_tokens(rd, reg, token or "wb-placeholder", a.dry)
    write_ports_file(home, reg, a.dry)
    sync_configs(home, reg, cfgs, a.dry)
    print("已写 config:", len(cfgs), "份")
    print("留底       :", b, "(dry-run)" if a.dry else "")
    print("新号生效   : supervisor 启动时读账号清单，新增 region 必须让它重读一次")
    print("             全新机器无需额外动作（init 已用清单驱动 supervisor）；")
    print("             已有旧版 supervisor 的机器见 README「一次性接管」，做一次换版+重启")
    print("收尾       : python3 wbctl.py sync  拉真实模型目录")
    print("             python3 wbctl.py audit 验收")


def cmd_sync(a):
    home = a.home
    reg = load_registry(home)
    cfgs = config_files(home)
    token = read_env_key(os.path.join(home, ".env"), KEY_ENV)
    changed = 0
    for x in reg["accounts"]:
        code, models = http_json("http://127.0.0.1:%d/v1/models" % x["port"], token)
        if code == 200 and models.get("data"):
            ids = [m["id"] for m in models["data"]]
            if ids != x.get("models"):
                x["models"] = ids
                changed += 1
                print("  刷新 %-18s %d 个模型" % (x["slug"], len(ids)))
        else:
            print("  %-18s bridge 未响应(%s)，保留原列表" % (x["slug"], code or "ERR"))
    if changed and not a.dry:
        save_registry(home, reg, a.dry)
    b = sync_configs(home, reg, cfgs, a.dry)
    print("同步完成：%d 个账号，%d 份 config，留底 %s%s"
          % (len(reg["accounts"]), len(cfgs), b, " (dry-run)" if a.dry else ""))


def cmd_list(a):
    reg = load_registry(a.home)
    if not reg["accounts"]:
        print("(空) 先跑 wbctl init")
        return
    token = read_env_key(os.path.join(a.home, ".env"), KEY_ENV)
    for x in reg["accounts"]:
        code, _ = http_json("http://127.0.0.1:%d/healthz" % x["port"], token, timeout=3)
        print("  %-18s %-8s port=%-5d %-4s account=%-8s models=%-3d health=%s"
              % (x["slug"], x["region"], x["port"], x["label"],
                 x["account"][:8], len(x.get("models") or []), code or "DOWN"))


def cmd_audit(a):
    home = a.home
    reg = load_registry(home)
    cfgs = config_files(home)
    token = read_env_key(os.path.join(home, ".env"), KEY_ENV)
    problems = []
    print("== 只读体检 ==")
    print("HERMES_HOME :", home)
    print("registry    :", len(reg["accounts"]), "个账号")
    print("config      :", len(cfgs), "份")
    print("%s 口令   : %s" % (KEY_ENV, "已设置" if token else "缺失"))
    if not token:
        problems.append("全局 .env 缺 " + KEY_ENV)
    for x in reg["accounts"]:
        code, _ = http_json("http://127.0.0.1:%d/v1/models" % x["port"], token, timeout=4)
        ok_cfg = []
        for c in cfgs:
            txt = open(c).read()
            ok_cfg.append(("api: http://127.0.0.1:%d/v1" % x["port"]) in txt
                          and re.search(r"^  " + re.escape(x["slug"]) + r":", txt, re.M) is not None)
        n_ok = sum(ok_cfg)
        print("  %-18s port=%-5d /v1/models=%-4s config=%d/%d %s"
              % (x["slug"], x["port"], code or "DOWN", n_ok, len(cfgs),
                 "OK" if code == 200 and n_ok == len(cfgs) else "PROBLEM"))
        if code != 200:
            problems.append("%s 的 bridge 不可达 (HTTP %s)" % (x["slug"], code or "ERR"))
        if n_ok != len(cfgs):
            problems.append("%s 只在 %d/%d 份 config 里" % (x["slug"], n_ok, len(cfgs)))
    envs = env_files(home)
    missing = [p for p in envs if not read_env_key(p, KEY_ENV)]
    if missing:
        problems.append("%d 个 .env 缺 %s" % (len(missing), KEY_ENV))
    print("  .env      : %d/%d 份有 %s" % (len(envs) - len(missing), len(envs), KEY_ENV))
    print("\n结论:", "全部通过" if not problems else "发现 %d 个问题" % len(problems))
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


def cmd_remove(a):
    home = a.home
    reg = load_registry(home)
    hit = [x for x in reg["accounts"] if x["slug"] == a.slug]
    if not hit:
        raise SystemExit("没有这个 slug: " + a.slug)
    reg["accounts"] = [x for x in reg["accounts"] if x["slug"] != a.slug]
    cfgs = config_files(home)
    b = backup(cfgs + [registry_path(home)], home, "remove-" + a.slug, a.dry)
    save_registry(home, reg, a.dry)
    prune_configs(home, a.slug, cfgs, a.dry)
    write_regions(runtime_dir(home), reg, a.dry)
    write_ports_file(home, reg, a.dry)
    print("已移除 %s，留底 %s%s" % (a.slug, b, " (dry-run)" if a.dry else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wbctl", description="WorkBuddy 多账号接入 Hermes")
    ap.add_argument("--home", default=DEFAULT_HOME, help="HERMES_HOME（默认 ~/.hermes）")
    ap.add_argument("--vault", help="WorkBuddy vault 目录")
    ap.add_argument("--dry-run", action="store_true", dest="dry", help="只算不写、不起进程")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="首次部署：探测 vault、生成 registry、写 config")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--regions", help="现有 regions.json，用于认领已有的账号与端口")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="加一个新账号（一条命令）")
    p.add_argument("--slug", help="如 workbuddy-4，省略则自动分配")
    p.add_argument("--account", help="vault 里账号 id 前缀，省略则自动挑未注册的")
    p.add_argument("--port", type=int)
    p.add_argument("--name")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("sync", help="重拉模型目录并把 registry 同步到所有 config")
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("list", help="列账号与健康")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("audit", help="只读体检")
    p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("remove", help="移除账号")
    p.add_argument("--slug", required=True)
    p.set_defaults(fn=cmd_remove)

    a = ap.parse_args(argv)
    rc = a.fn(a)
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
