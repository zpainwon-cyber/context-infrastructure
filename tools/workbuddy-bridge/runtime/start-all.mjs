// Supervisor: keeps one shim+bridge pair per region alive.
//
//   cn     -> bridge on 8765  -> https://copilot.tencent.com
//   global -> bridge on 8766  -> https://www.workbuddy.ai
//
// shim.mjs must be up before its bridge can serve, and its port/token live in
// shim-state.<region>.json, so each pair starts in order and restarts on exit.
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
// Registry is the single source of truth. `wbctl add` rewrites it, so a new
// account no longer needs a source edit here (that was the whole afternoon).
const HOME = process.env.WB_HOME ?? path.dirname(HERE);
const ACCOUNTS = (() => {
  for (const p of [path.join(HOME, "workbuddy-accounts.json"),
                   path.join(HERE, "accounts.json")]) {
    try {
      const reg = JSON.parse(fs.readFileSync(p, "utf8"));
      if (reg.accounts?.length) return reg.accounts;
    } catch { /* fall through to legacy */ }
  }
  // Legacy fallback for an install that has not run `wbctl init` yet.
  try {
    const legacy = JSON.parse(fs.readFileSync(path.join(HERE, "regions.json"), "utf8"));
    const defaults = { cn: 8765, global: 8766, cn2: 8767, cn3: 8768 };
    return Object.entries(legacy).map(([region, cfg]) => ({
      region, vault: cfg.vault, account: cfg.account,
      port: defaults[region] ?? 8769,
    }));
  } catch { return []; }
})();
const REGIONS = (process.env.WB_REGIONS ?? ACCOUNTS.map((a) => a.region).join(","))
  .split(",")
  .map((r) => r.trim())
  .filter(Boolean);
const PORTS = Object.fromEntries(ACCOUNTS.map((a) => [a.region, a.port]));
const REGION_CONFIG = Object.fromEntries(
  ACCOUNTS.map((a) => [a.region, { vault: a.vault, account: a.account }]));
const regionEnv = (region) => {
  const cfg = REGION_CONFIG[region] ?? {};
  return {
    WB_REGION: region,
    ...(cfg.vault && cfg.vault !== region ? { WB_VAULT_REGION: cfg.vault } : {}),
    ...(cfg.account ? { WB_ACCOUNT_ID: cfg.account } : {}),
  };
};

let stopping = false;
const children = new Map();

const stateFileFor = (region) => path.join(HERE, `shim-state.${region}.json`);

function start(key, script, env) {
  const child = spawn(process.execPath, [path.join(HERE, script)], {
    cwd: HERE,
    env: { ...process.env, ...env },
    stdio: ["ignore", "inherit", "inherit"],
  });
  children.set(key, child);
  console.log(`[supervisor] started ${key} pid=${child.pid}`);

  child.on("exit", (code, signal) => {
    children.delete(key);
    if (stopping) return;
    console.log(`[supervisor] ${key} exited (code=${code} signal=${signal}); restarting in 3s`);
    // A restarted shim gets a new port and token; its bridge re-reads the state
    // file on every request, so it needs no restart of its own.
    setTimeout(() => start(key, script, env), 3000);
  });
  return child;
}

async function waitForShim(region, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const state = JSON.parse(fs.readFileSync(stateFileFor(region), "utf8"));
      process.kill(state.pid, 0); // throws when the pid is gone
      return state;
    } catch {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  return null;
}

function stopAll() {
  stopping = true;
  for (const [key, child] of children) {
    console.log(`[supervisor] stopping ${key}`);
    child.kill("SIGTERM");
  }
  setTimeout(() => process.exit(0), 1500);
}

process.on("SIGINT", stopAll);
process.on("SIGTERM", stopAll);

for (const region of REGIONS) {
  // Stale state from a previous run must not fool waitForShim.
  try {
    fs.unlinkSync(stateFileFor(region));
  } catch {
    /* no state file yet */
  }

  start(`shim:${region}`, "shim.mjs", regionEnv(region));
  const state = await waitForShim(region);
  if (state === null) {
    console.log(`[supervisor] ${region}: shim never reported ready — skipping its bridge`);
    continue;
  }
  start(`bridge:${region}`, "bridge.mjs", { ...regionEnv(region), WB_PORT: String(PORTS[region] ?? 8767) });
  console.log(
    `[supervisor] ${region} ready: shim ${state.baseUrl} (${state.models?.length ?? "?"} models) -> bridge 127.0.0.1:${PORTS[region] ?? 8767}`,
  );
}

console.log("[supervisor] all up");
