// Stable front door for the WorkBuddy bridge.
//
//   Hermes -> 127.0.0.1:8765/v1 (fixed port + fixed bearer)
//          -> shim.mjs (random port + random bearer, in-process)
//          -> WorkBuddyUpstreamClient -> copilot.tencent.com / *.workbuddy.ai
//
// The shim is OpenAI-compatible but always answers with SSE, and its port and
// token change on every start. This layer fixes both, and aggregates SSE into
// a single JSON body when the caller asked for stream:false.
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const REGION = process.env.WB_REGION ?? "cn";
// Region id for ports/state/token vs the vault directory its credential lives in.
// A second account in one region is `cn2`: same vault, WB_ACCOUNT_ID pins the account.
const VAULT_REGION = process.env.WB_VAULT_REGION ?? REGION;
const SHIM_STATE = path.join(HERE, `shim-state.${REGION}.json`);
const TOKEN_FILE = path.join(HERE, `token.${REGION}`);
const PORT = Number(process.env.WB_PORT ?? (REGION === "global" ? 8766 : 8765));

if (!fs.existsSync(TOKEN_FILE)) {
  fs.writeFileSync(TOKEN_FILE, "wb-" + crypto.randomBytes(24).toString("base64url"), { mode: 0o600 });
}
const TOKEN = fs.readFileSync(TOKEN_FILE, "utf8").trim();

function readShim() {
  return JSON.parse(fs.readFileSync(SHIM_STATE, "utf8"));
}

// Model ids are advertised with the upstream credit multiplier appended
// ("glm-5.3-flash · x0.08") so the picker shows the price where the user picks.
// The wire id is the part before the separator; everything after it is display.
const DECOR = " · ";
const stripDecor = (id) => String(id ?? "").split("·")[0].trim();

// ── credits ────────────────────────────────────────────────────────────────
// Balance + check-in status for this region's vaulted account, cached 15s so a
// UI chip polling it does not hammer the billing endpoint. Multipliers come
// from the shim's own catalog snapshot, no upstream call needed.
const VAULT_ROOT = process.env.WB_VAULT_DIR ?? path.join(process.env.HOME, ".dsh", "workbuddy-vault");
let creditsCache = { at: 0, value: null };

function loadVaultCredential(region) {
  const dir = path.join(VAULT_ROOT, region);
  if (!fs.existsSync(dir)) return undefined;
  const files = fs.readdirSync(dir).filter((f) => f.endsWith(".json"));
  if (files.length === 0) return undefined;
  const docs = files
    .map((f) => ({ doc: JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")), file: f }))
    .sort((a, b) => (b.doc.refreshedAtMs ?? 0) - (a.doc.refreshedAtMs ?? 0));
  // Same pin as shim.mjs: a cn2-style region reads the cn vault but owns ONE account.
  const pin = (process.env.WB_ACCOUNT_ID ?? "").trim().toLowerCase();
  const picked = pin
    ? docs.find(({ doc, file }) =>
        String(doc.accountId ?? "").toLowerCase().endsWith(pin) || file.toLowerCase().startsWith(pin))
    : docs[0];
  if (!picked) return undefined;
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

async function creditsPayload() {
  if (creditsCache.value !== null && Date.now() - creditsCache.at < 15_000) return creditsCache.value;
  const shim = readShim();
  const models = (shim.models ?? []).map((m) =>
    typeof m === "string" ? { id: m } : { id: m.id, credit: m.credit ?? m.creditMultiplier },
  );
  const value = { region: REGION, models, total: null, packages: [], checkin: null, account: null, error: null };
  try {
    const credential = loadVaultCredential(VAULT_REGION);
    if (!credential) throw new Error(`no credential for ${REGION}`);
    const { WorkBuddyUpstreamClient } = await import("dsh-connect-workbuddy");
    const client = new WorkBuddyUpstreamClient(() => {}, {});
    const credits = await client.fetchCredits(credential);
    value.total = credits?.total ?? null;
    value.packages = credits?.packages ?? [];
    value.account = credential.nickname || credential.displayName || credential.uid;
    try {
      const checkin = await client.fetchCheckinStatus(credential);
      value.checkin = { claimed: checkin.todayCheckedIn, credit: checkin.todayCredit, button: checkin.claimButtonText };
    } catch {
      /* global region has no check-in */
    }
  } catch (error) {
    value.error = String(error?.message ?? error);
  }
  creditsCache = { at: Date.now(), value };
  return value;
}


function authorized(req) {
  const header = req.headers.authorization ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (match === null) return false;
  const bearer = match[1];
  if (bearer === TOKEN) return true;
  // One front door per account is a historical split, not an isolation boundary:
  // the account is chosen by THIS instance's shim-state.<region>.json + vault, and
  // a restored session can still carry the previous account's local password after
  // the picker re-pointed base_url. Accept any token issued by this host's bridges
  // so the request lands on the picked account instead of dying as a 401. Upstream
  // credentials stay account-scoped and never cross regions.
  for (const file of fs.readdirSync(HERE)) {
    if (!/^token\./.test(file)) continue;
    try {
      if (fs.readFileSync(path.join(HERE, file), "utf8").trim() === bearer) return true;
    } catch { /* a token file mid-write */ }
  }
  return false;
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
    // The desktop renderer fetches /credits from an app:// origin.
    "Access-Control-Allow-Origin": "*",
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

// Turn a WorkBuddy SSE stream into one OpenAI chat.completion object.
function aggregate(id, model, chunks) {
  let content = "";
  let reasoning = "";
  let finish = null;
  let usage = null;
  let toolCalls = [];
  for (const chunk of chunks) {
    if (chunk.usage) usage = chunk.usage;
    const choice = (chunk.choices ?? [])[0];
    if (!choice) continue;
    const delta = choice.delta ?? {};
    if (typeof delta.content === "string") content += delta.content;
    if (typeof delta.reasoning_content === "string") reasoning += delta.reasoning_content;
    if (Array.isArray(delta.tool_calls) && delta.tool_calls.length > 0) {
      for (const tc of delta.tool_calls) {
        const idx = tc.index ?? 0;
        toolCalls[idx] ??= { id: tc.id ?? `call_${idx}`, type: "function", function: { name: "", arguments: "" } };
        if (tc.id) toolCalls[idx].id = tc.id;
        if (tc.function?.name) toolCalls[idx].function.name += tc.function.name;
        if (tc.function?.arguments) toolCalls[idx].function.arguments += tc.function.arguments;
      }
    }
    if (choice.finish_reason) finish = choice.finish_reason;
  }
  const message = { role: "assistant", content };
  if (reasoning !== "") message.reasoning_content = reasoning;
  if (toolCalls.length > 0) message.tool_calls = toolCalls.filter(Boolean);
  return {
    id,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, message, finish_reason: finish ?? (toolCalls.length > 0 ? "tool_calls" : "stop") }],
    usage: usage ?? undefined,
  };
}

function parseSse(buffer) {
  const chunks = [];
  for (const line of buffer.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice(5).trim();
    if (payload === "" || payload === "[DONE]") continue;
    try {
      chunks.push(JSON.parse(payload));
    } catch {
      /* keep-alive or comment line */
    }
  }
  return chunks;
}

const server = http.createServer(async (req, res) => {
  try {
    const url = req.url ?? "/";
    // Read-only, loopback-only: the UI chip needs it without holding the chat token.
    if (req.method === "GET" && (url === "/credits" || url === "/credits/")) {
      sendJson(res, 200, await creditsPayload());
      return;
    }
    if (url === "/healthz") {
      const shim = readShim();
      sendJson(res, 200, { ok: true, shim: shim.baseUrl, region: shim.region, models: shim.models?.length ?? 0 });
      return;
    }
    if (!authorized(req)) {
      sendJson(res, 401, { error: { message: "invalid bridge token", type: "auth" } });
      return;
    }
    const shim = readShim();

    if (req.method === "GET" && (url === "/v1/models" || url === "/v1/models/")) {
      const upstream = await fetch(`${shim.baseUrl}/v1/models`, { headers: { Authorization: `Bearer ${shim.token}` } });
      if (!upstream.ok) {
        res.writeHead(upstream.status, { "Content-Type": "application/json" });
        res.end(await upstream.text());
        return;
      }
      const catalog = await upstream.json();
      const creditOf = new Map(
        (shim.models ?? []).map((m) => (typeof m === "string" ? [m, undefined] : [m.id, m.credit ?? m.creditMultiplier])),
      );
      const data = (catalog.data ?? []).map((model) => {
        const credit = creditOf.get(model.id);
        return typeof credit === "number" && Number.isFinite(credit)
          ? { ...model, id: `${model.id}${DECOR}x${credit}` }
          : model;
      });
      sendJson(res, 200, { ...catalog, data });
      return;
    }

    if (req.method === "POST" && url.startsWith("/v1/chat/completions")) {
      const raw = await readBody(req);
      let payload;
      try {
        payload = JSON.parse(raw.toString("utf8"));
      } catch {
        sendJson(res, 400, { error: { message: "request body is not JSON", type: "invalid_request" } });
        return;
      }
      const wantsStream = payload.stream === true;
      const model = stripDecor(payload.model ?? "unknown");
      const upstream = await fetch(`${shim.baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: { Authorization: `Bearer ${shim.token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, model, stream: true }),
      });

      if (!upstream.ok) {
        const body = await upstream.text();
        res.writeHead(upstream.status, { "Content-Type": "application/json" });
        res.end(body);
        return;
      }

      if (wantsStream) {
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
          "X-Accel-Buffering": "no",
        });
        const reader = upstream.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          res.write(value);
        }
        res.end();
        return;
      }

      const text = await upstream.text();
      const chunks = parseSse(text);
      if (chunks.length === 0) {
        sendJson(res, 502, { error: { message: "upstream returned an empty stream", type: "upstream" } });
        return;
      }
      const first = chunks[0];
      sendJson(res, 200, aggregate(first.id ?? `chatcmpl-${Date.now()}`, first.model ?? model, chunks));
      return;
    }

    if (req.method === "GET" && url === "/") {
      sendJson(res, 200, { service: "workbuddy-hermes-bridge", port: PORT });
      return;
    }

    sendJson(res, 404, { error: { message: `no route for ${req.method} ${url}`, type: "invalid_request" } });
  } catch (error) {
    if (!res.headersSent) sendJson(res, 502, { error: { message: String(error?.message ?? error), type: "transport" } });
    else res.end();
  }
});

server.listen(PORT, "127.0.0.1", () => {
  const shim = readShim();
  console.log(`bridge    http://127.0.0.1:${PORT}/v1`);
  console.log(`token     ${TOKEN.slice(0, 10)}… (${TOKEN_FILE})`);
  console.log(`shim      ${shim.baseUrl} (${shim.region})`);
});
