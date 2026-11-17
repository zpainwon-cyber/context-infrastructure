"""Real execution tests: build an isolated HERMES_HOME, then actually run wbctl."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings

warnings.filterwarnings("ignore", category=ResourceWarning)

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)
import wbctl

CFG = """model:
  default: deepseek-v4.1-flash

providers:
  workbuddy:
    name: WorkBuddy
    api: http://127.0.0.1:8765/v1
    key_env: WB_SHIM_TOKEN
    transport: openai
    discover_models: false
    models:
      - hy4free

other:
  keep: me
"""


def acct(slug, region, port, label, aid):
    return {"slug": slug, "name": slug, "region": region, "vault": "cn",
            "account": aid, "port": port, "label": label, "models": ["m · x0.1"]}


class TestYaml(unittest.TestCase):
    def test_scalar_quotes_risky(self):
        self.assertEqual(wbctl.yaml_scalar("hy4std · x0.29"), "hy4std · x0.29")
        self.assertEqual(wbctl.yaml_scalar("- bad"), "'- bad'")
        self.assertEqual(wbctl.yaml_scalar("a: b"), "'a: b'")

    def test_insert_not_nested(self):
        out = wbctl.upsert_provider(CFG, "workbuddy-4",
                                    wbctl.provider_block("workbuddy-4", "W4", 8769, ["m1"]))
        self.assertIn("  workbuddy-4:", out)
        self.assertNotIn("\n    workbuddy-4:", out)   # 4-space nesting bug
        self.assertIn("other:", out)
        self.assertIn("api: http://127.0.0.1:8769/v1", out)
        self.assertIn("key_env: WB_SHIM_TOKEN", out)

    def test_replace_is_idempotent(self):
        blk = wbctl.provider_block("workbuddy-4", "W4", 8769, ["m1"])
        a = wbctl.upsert_provider(CFG, "workbuddy-4", blk)
        b = wbctl.upsert_provider(a, "workbuddy-4", blk)
        self.assertEqual(a, b)
        self.assertEqual(b.count("  workbuddy-4:"), 1)

    def test_remove(self):
        a = wbctl.upsert_provider(CFG, "workbuddy-4",
                                  wbctl.provider_block("workbuddy-4", "W4", 8769, ["m1"]))
        b = wbctl.remove_provider(a, "workbuddy-4")
        self.assertNotIn("workbuddy-4", b)
        self.assertIn("  workbuddy:", b)
        self.assertIn("other:", b)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wbctl-test-")
        self.home = os.path.join(self.tmp, "hermes")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.home, "profiles", "p1"))
        os.makedirs(os.path.join(self.home, "profiles", "p2"))
        os.makedirs(os.path.join(self.home, "workbuddy-bridge",
                                 "node_modules", "dsh-connect-workbuddy"))
        for d in ("cn", "global"):
            os.makedirs(os.path.join(self.vault, d))
        self.cfgs = [os.path.join(self.home, "config.yaml"),
                     os.path.join(self.home, "profiles", "p1", "config.yaml"),
                     os.path.join(self.home, "profiles", "p2", "config.yaml")]
        for c in self.cfgs:
            with open(c, "w") as f:
                f.write(CFG)
        with open(os.path.join(self.home, ".env"), "w") as f:
            f.write("WB_SHIM_TOKEN=test-local-token\n")
        for p in ("p1", "p2"):
            with open(os.path.join(self.home, "profiles", p, ".env"), "w") as f:
                f.write("WB_SHIM_TOKEN=test-local-token\n")
        # 3 registered cn accounts + 2 spare vault accounts + global
        for i, aid in enumerate(["aaaaaaaa1111", "bbbbbbbb2222", "cccccccc3333",
                                 "dddddddd4444", "eeeeeeee5555"]):
            with open(os.path.join(self.vault, "cn", "%d.json" % i), "w") as f:
                json.dump({"accountId": aid, "nickname": "nick%d" % i}, f)
        with open(os.path.join(self.vault, "global", "g.json"), "w") as f:
            json.dump({"accountId": "gggggggg9999", "nickname": "global"}, f)
        reg = {"version": 1, "accounts": [
            acct("workbuddy", "cn", 8765, "CN", "aaaaaaaa1111"),
            acct("workbuddy-2", "cn2", 8767, "CN2", "bbbbbbbb2222"),
            acct("workbuddy-3", "cn3", 8768, "CN3", "cccccccc3333"),
        ]}
        with open(wbctl.registry_path(self.home), "w") as f:
            json.dump(reg, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        cmd = [sys.executable, os.path.join(PLUGIN, "wbctl.py"),
               "--home", self.home, "--vault", self.vault] + list(args)
        env = dict(os.environ, WBCTL_OFFLINE="1")
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)

    def test_add_writes_every_config(self):
        r = self.run_cli("add", "--slug", "workbuddy-4")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for c in self.cfgs:
            t = open(c).read()
            self.assertIn("  workbuddy-4:", t, c)
            self.assertIn("api: http://127.0.0.1:8769/v1", t, c)
            self.assertIn("key_env: WB_SHIM_TOKEN", t, c)
            self.assertNotIn("\n    workbuddy-4:", t, c)
            self.assertIn("  workbuddy:", t, c)       # old ones intact
            self.assertIn("other:", t, c)
        reg = json.load(open(wbctl.registry_path(self.home)))
        self.assertEqual(len(reg["accounts"]), 4)
        self.assertEqual(reg["accounts"][-1]["port"], 8769)
        self.assertEqual(reg["accounts"][-1]["region"], "cn4")
        self.assertEqual(reg["accounts"][-1]["account"], "dddddddd4444")
        ports = json.load(open(os.path.join(self.home, "workbuddy-ports.json")))
        self.assertEqual(ports["workbuddy-4"]["port"], 8769)
        regions = json.load(open(os.path.join(self.home, "workbuddy-bridge", "regions.json")))
        self.assertIn("cn4", regions)

    def test_add_is_idempotent_on_repeat(self):
        self.assertEqual(self.run_cli("add", "--slug", "workbuddy-4").returncode, 0)
        r = self.run_cli("add", "--slug", "workbuddy-4")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(json.load(open(wbctl.registry_path(self.home)))["accounts"]), 4)

    def test_audit_is_readonly(self):
        before = {c: hashlib.sha256(open(c, "rb").read()).hexdigest() for c in self.cfgs}
        self.run_cli("add", "--slug", "workbuddy-4")
        r = self.run_cli("audit")
        self.assertIn("只读体检", r.stdout)
        # audit must never mutate config
        after = {c: hashlib.sha256(open(c, "rb").read()).hexdigest() for c in self.cfgs}
        self.assertEqual(before == after, False)  # add changed them, that's expected
        snap = dict(after)
        self.run_cli("audit")
        self.assertEqual(snap, {c: hashlib.sha256(open(c, "rb").read()).hexdigest()
                                for c in self.cfgs})

    def test_auto_pick_and_fifth_account(self):
        self.assertEqual(self.run_cli("add").returncode, 0)          # auto slug+account
        r = self.run_cli("add")
        self.assertEqual(r.returncode, 0)
        reg = json.load(open(wbctl.registry_path(self.home)))
        self.assertEqual(len(reg["accounts"]), 5)
        self.assertEqual([a["port"] for a in reg["accounts"]], [8765, 8767, 8768, 8769, 8770])
        self.assertIn("workbuddy-5", [a["slug"] for a in reg["accounts"]])

    def test_init_keeps_existing_models(self):
        # real config already carries a model list; init must not blank it
        r = self.run_cli("init", "--force", "--no-start")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for c in self.cfgs:
            t = open(c).read()
            self.assertIn("      - hy4free", t, c)
        reg = json.load(open(wbctl.registry_path(self.home)))
        main = [a for a in reg["accounts"] if a["slug"] == "workbuddy"][0]
        self.assertTrue(main.get("models"), "init 把主号模型列表清空了: %s" % main)

    def test_hidden_profile_ignored(self):
        dead = os.path.join(self.home, "profiles", ".deleted")
        os.makedirs(dead)
        with open(os.path.join(dead, "config.yaml"), "w") as f:
            f.write(CFG)
        with open(os.path.join(dead, ".env"), "w") as f:
            f.write("WB_SHIM_TOKEN=x\n")
        self.assertEqual(len(wbctl.config_files(self.home)), 3)
        self.assertEqual(len(wbctl.env_files(self.home)), 3)

    def test_dry_run_writes_nothing(self):
        before = {c: hashlib.sha256(open(c, "rb").read()).hexdigest() for c in self.cfgs}
        r = self.run_cli("--dry-run", "add", "--slug", "workbuddy-4")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(before, {c: hashlib.sha256(open(c, "rb").read()).hexdigest()
                                  for c in self.cfgs})
        self.assertEqual(len(json.load(open(wbctl.registry_path(self.home)))["accounts"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
