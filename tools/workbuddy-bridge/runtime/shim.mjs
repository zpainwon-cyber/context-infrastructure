// WorkBuddy -> Hermes bridge.
// Reuses dsh-connect-workbuddy's own upstream client + OpenAI-compatible shim
// (the package is MIT) so Hermes can talk to WorkBuddy models over a plain
// localhost OpenAI endpoint. No DSH host required.
import fs from "node:fs";
import path from "node:path";
import {
  WorkBuddyUpstreamClient,
  createWorkBuddyShim,
  FALLBACK_WORKBUDDY_MODELS,
  FALLBACK_WORKBUDDY_MODELS_GLOBAL,
  deriveCatalog,
} from "dsh-connect-workbuddy";

const REGION = process.env.WB_REGION ?? "cn";
// A second account in the SAME region is a distinct region id (cn2) reading the
// same vault directory, pinned to one account. Without the pin the loader takes
// whichever credential refreshed last, so two accounts silently trade places.
const VAULT_REGION = process.env.WB_VAULT_REGION ?? REGION;
const ACCOUNT_PIN = (process.env.WB_ACCOUNT_ID ?? "").trim();
const VAULT_ROOT = process.env.WB_VAULT_DIR ?? path.join(process.env.HOME, ".dsh", "workbuddy-vault");
const HERE = path.dirname(new URL(import.meta.url).pathname);

function loadVault(region) {
  const dir = path.join(VAULT_ROOT, region);
  if (!fs.existsSync(dir)) return undefined;
  const files = fs.readdirSync(dir).filter((f) => f.endsWith(".json"));
  if (files.length === 0) return undefined;
  const docs = files.map((f) => ({ doc: JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")), file: f }));
  docs.sort((a, b) => (b.doc.refreshedAtMs ?? 0) - (a.doc.refreshedAtMs ?? 0));
  const picked = ACCOUNT_PIN
    ? docs.find(({ doc, file }) =>
        String(doc.accountId ?? "").toLowerCase().endsWith(ACCOUNT_PIN.toLowerCase()) ||
        file.toLowerCase().startsWith(ACCOUNT_PIN.toLowerCase()))
    : docs[0];
  if (!picked && ACCOUNT_PIN) {
    console.error(`no credential matching WB_ACCOUNT_ID=${ACCOUNT_PIN} in ${dir} (have: ${files.join(", ")})`);
    return undefined;
  }
  const doc = picked.doc;
  return {
    accountId: doc.accountId,
    accessToken: doc.auth.accessToken,
    refreshToken: doc.auth.refreshToken,
    expiresAt: doc.auth.expiresAt,
    refreshExpiresAt: doc.auth.refreshExpiresAt,
    domain: doc.auth.domain,
    uid: doc.account.uid ?? "",
    uin: doc.account.uin,
    nickname: doc.account.nickname ?? "",
    enterpriseId: doc.account.enterpriseId,
    displayName: doc.account.displayName,
  };
}

const credential = loadVault(VAULT_REGION);
if (!credential) {
  console.error(`no WorkBuddy credential for region ${REGION} under ${VAULT_ROOT}`);
  process.exit(1);
}

const client = new WorkBuddyUpstreamClient((msg) => console.error("[warn]", msg), {});

const fallback = REGION === "global" ? FALLBACK_WORKBUDDY_MODELS_GLOBAL : FALLBACK_WORKBUDDY_MODELS;
let live = [];
let models = deriveCatalog(fallback, new Set(), {});

async function refreshCatalog() {
  try {
    const roster = await client.fetchModels(credential);
    if (Array.isArray(roster) && roster.length > 0) {
      live = roster;
      models = deriveCatalog(roster, new Set(), {});
      console.log(`catalog refreshed: ${models.length} models`);
    }
  } catch (error) {
    console.error("[warn] live catalog refresh failed:", error?.message ?? error);
  }
}
await refreshCatalog();

const store = { resolve: async () => credential, accounts: async () => [credential] };
const catalog = { current: () => models };

const shim = createWorkBuddyShim({ store, client, catalog, logger: console });
await shim.ready;

const baseUrl = shim.baseUrl();
const token = shim.token();
const stateFile = path.join(HERE, `shim-state.${REGION}.json`);
fs.writeFileSync(
  stateFile,
  JSON.stringify({ region: REGION, baseUrl, token, endpoint: `${baseUrl}/v1`, pid: process.pid, catalogLive: models.length > 0, models: models.map((m) => ({ id: m.id, credit: m.creditMultiplier })) }, null, 2),
);

console.log(`[${REGION}] region    ${REGION}`);
console.log(`endpoint  ${baseUrl}/v1`);
console.log(`token     ${token.slice(0, 8)}… (${token.length} chars)`);
console.log(`models    ${models.map((m) => `${m.id}${m.creditMultiplier === undefined ? "" : `·x${m.creditMultiplier}`}`).join(", ")}`);
console.log(`state     ${stateFile}`);

// keep the roster fresh in the background; cheap read, same identity as chat
setInterval(refreshCatalog, 30 * 60 * 1000).unref();

const shutdown = async () => { await shim.close(); process.exit(0); };
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
