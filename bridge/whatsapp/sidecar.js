/*
 * Vira WhatsApp sidecar — a linked device speaking the multi-device
 * protocol (Baileys, pinned in package.json). Receive-only v1: it never
 * sends a message. The Vira server (or the owner, on a passive test
 * instance) starts this process and hands it every path on argv; the
 * sidecar itself decides nothing about where state lives.
 *
 *   node sidecar.js --port 18377 \
 *     --session-dir <data>/whatsapp/session \
 *     --inbox <data>/whatsapp/inbox.ndjson \
 *     [--pidfile <data>/whatsapp/sidecar.pid] [--log <data>/whatsapp/sidecar.log]
 *
 * Surface (binds 127.0.0.1 ONLY — message content never leaves the machine):
 *   GET /status            {connected, jid, needs_pair, logged_out, ...}
 *   GET /qr                {qr, png} while pairing; nulls once linked
 *   GET /messages?after=N  inbox NDJSON lines past byte-offset N -> {messages, cursor}
 *   POST /stop             graceful exit
 *
 * Inbound messages append to the inbox file as NDJSON; the byte offset is
 * the poll cursor, so a sidecar restart never invalidates Vira's cursor.
 * The session directory holds the linked-device credentials — deleting it
 * unlinks the device and forces a re-pair (same class of fragility as the
 * venv/FDA grant: never "clean it up").
 */
"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");

const pino = require("pino");
const QRCode = require("qrcode");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  DisconnectReason,
  Browsers,
} = require("baileys");

// ---------- args ----------

function arg(name, fallback) {
  const i = process.argv.indexOf("--" + name);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const PORT = parseInt(arg("port", "18377"), 10);
const SESSION_DIR = arg("session-dir", null);
const INBOX = arg("inbox", null);
const PIDFILE = arg("pidfile", null);
const LOGFILE = arg("log", null);

if (!SESSION_DIR || !INBOX) {
  console.error("usage: node sidecar.js --port N --session-dir DIR --inbox FILE");
  process.exit(2);
}

const logger = pino(
  { level: arg("log-level", "warn") },
  LOGFILE ? pino.destination({ dest: LOGFILE, sync: false }) : undefined,
);

// ---------- state ----------

const state = {
  connected: false,
  jid: null,
  qr: null, // latest pairing QR string, null once linked
  logged_out: false,
  last_event: null,
  started: new Date().toISOString(),
  messages_seen: 0,
};

let sock = null;
let stopping = false;
const groupNames = new Map(); // jid -> subject (best-effort cache)

function ownerOnly(p, mode) {
  try {
    fs.chmodSync(p, mode);
  } catch {
    /* Windows: no POSIX modes — the profile dir is already per-user */
  }
}

function ensureFiles() {
  fs.mkdirSync(SESSION_DIR, { recursive: true });
  ownerOnly(SESSION_DIR, 0o700);
  fs.mkdirSync(path.dirname(INBOX), { recursive: true });
  if (!fs.existsSync(INBOX)) fs.writeFileSync(INBOX, "");
  ownerOnly(INBOX, 0o600);
  if (PIDFILE) fs.writeFileSync(PIDFILE, String(process.pid));
}

// ---------- message shaping ----------

function unwrap(message) {
  // Peel the transport wrappers so kind detection sees the real content.
  let m = message;
  for (let i = 0; i < 4 && m; i++) {
    if (m.ephemeralMessage) m = m.ephemeralMessage.message;
    else if (m.viewOnceMessage) m = m.viewOnceMessage.message;
    else if (m.viewOnceMessageV2) m = m.viewOnceMessageV2.message;
    else if (m.documentWithCaptionMessage) m = m.documentWithCaptionMessage.message;
    else break;
  }
  return m || {};
}

function classify(m) {
  if (m.conversation || m.extendedTextMessage) return "text";
  if (m.imageMessage) return "image";
  if (m.videoMessage) return "video";
  if (m.audioMessage) return m.audioMessage.ptt ? "voice" : "audio";
  if (m.documentMessage) return "document";
  if (m.stickerMessage) return "sticker";
  if (m.locationMessage || m.liveLocationMessage) return "location";
  if (m.contactMessage || m.contactsArrayMessage) return "contact";
  if (m.pollCreationMessage || m.pollCreationMessageV2 || m.pollCreationMessageV3)
    return "poll";
  return null; // protocol noise (reactions, receipts, key changes): skip
}

function textOf(m, kind) {
  switch (kind) {
    case "text":
      return m.conversation || (m.extendedTextMessage || {}).text || "";
    case "image":
      return (m.imageMessage || {}).caption || "";
    case "video":
      return (m.videoMessage || {}).caption || "";
    case "document":
      return (m.documentMessage || {}).fileName || "";
    case "location":
      return ((m.locationMessage || m.liveLocationMessage) || {}).name || "";
    case "poll": {
      const p = m.pollCreationMessage || m.pollCreationMessageV2 || m.pollCreationMessageV3;
      return (p || {}).name || "";
    }
    default:
      return "";
  }
}

async function groupSubject(jid) {
  if (groupNames.has(jid)) return groupNames.get(jid);
  try {
    const md = await sock.groupMetadata(jid);
    groupNames.set(jid, md.subject || null);
  } catch {
    groupNames.set(jid, null); // unknown stays unknown; retried only on restart
  }
  return groupNames.get(jid);
}

async function shape(msg) {
  const key = msg.key || {};
  const chatJid = key.remoteJid || "";
  if (!chatJid || chatJid === "status@broadcast" || chatJid.endsWith("@newsletter"))
    return null; // stories and channels are not conversations
  const m = unwrap(msg.message);
  const kind = classify(m);
  if (!kind) return null;
  const group = chatJid.endsWith("@g.us");
  const senderJid = group ? key.participant || "" : chatJid;
  const row = {
    id: key.id || "",
    chat_jid: chatJid,
    sender_jid: senderJid,
    // When WhatsApp hides the number behind a LID, newer servers attach the
    // real E.164 alongside; carry it when present so Vira can join the CRM.
    sender_pn: key.senderPn || key.participantPn || null,
    from_me: !!key.fromMe,
    timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
    kind,
    text: (textOf(m, kind) || "").slice(0, 2000),
    push_name: msg.pushName || null,
    group,
    group_subject: group ? await groupSubject(chatJid) : null,
  };
  return row;
}

function appendInbox(row) {
  fs.appendFileSync(INBOX, JSON.stringify(row) + "\n");
  state.messages_seen += 1;
}

// ---------- WhatsApp connection ----------

async function connect() {
  const { state: auth, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch {
    version = undefined; // offline: the library's bundled version list
  }

  sock = makeWASocket({
    version,
    auth: {
      creds: auth.creds,
      keys: makeCacheableSignalKeyStore(auth.keys, logger),
    },
    logger,
    browser: Browsers.macOS("Desktop"),
    printQRInTerminal: false,
    // Stay quiet: never present as online (keeps phone notifications
    // intact) and never pull the full history — live messages only.
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    state.last_event = new Date().toISOString();
    if (u.qr) state.qr = u.qr;
    if (u.connection === "open") {
      state.connected = true;
      state.qr = null;
      state.jid = (sock.user && sock.user.id) || null;
      // Session files hold the account keys; keep them owner-only.
      try {
        for (const f of fs.readdirSync(SESSION_DIR)) ownerOnly(path.join(SESSION_DIR, f), 0o600);
      } catch { /* best-effort */ }
    }
    if (u.connection === "close") {
      state.connected = false;
      const code =
        u.lastDisconnect && u.lastDisconnect.error && u.lastDisconnect.error.output
          ? u.lastDisconnect.error.output.statusCode
          : 0;
      if (code === DisconnectReason.loggedOut) {
        // The phone unlinked us: credentials are dead. Stop reconnecting
        // and report it; re-pairing is an owner action.
        state.logged_out = true;
        logger.warn("logged out by phone — re-pair required");
      } else if (!stopping) {
        setTimeout(() => connect().catch((e) => logger.error(e, "reconnect failed")), 3000);
      }
    }
  });

  sock.ev.on("messages.upsert", async (ev) => {
    if (ev.type !== "notify" && ev.type !== "append") return;
    for (const msg of ev.messages || []) {
      try {
        const row = await shape(msg);
        if (row) appendInbox(row);
      } catch (e) {
        logger.error(e, "message shaping failed");
      }
    }
  });
}

// ---------- local HTTP seam ----------

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "content-type": "application/json" });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  try {
    if (req.method === "GET" && url.pathname === "/status") {
      return json(res, 200, {
        connected: state.connected,
        jid: state.jid,
        needs_pair: !state.connected && !!state.qr,
        logged_out: state.logged_out,
        last_event: state.last_event,
        started: state.started,
        messages_seen: state.messages_seen,
        inbox_bytes: fs.existsSync(INBOX) ? fs.statSync(INBOX).size : 0,
      });
    }
    if (req.method === "GET" && url.pathname === "/qr") {
      if (!state.qr) return json(res, 200, { qr: null, png: null });
      const png = await QRCode.toDataURL(state.qr, { margin: 1, width: 360 });
      return json(res, 200, { qr: state.qr, png });
    }
    if (req.method === "GET" && url.pathname === "/messages") {
      const after = Math.max(0, parseInt(url.searchParams.get("after") || "0", 10) || 0);
      const size = fs.existsSync(INBOX) ? fs.statSync(INBOX).size : 0;
      if (after >= size) return json(res, 200, { messages: [], cursor: size });
      const fd = fs.openSync(INBOX, "r");
      let chunk;
      try {
        const len = Math.min(size - after, 4 * 1024 * 1024);
        chunk = Buffer.alloc(len);
        fs.readSync(fd, chunk, 0, len, after);
      } finally {
        fs.closeSync(fd);
      }
      const text = chunk.toString("utf8");
      const complete = text.lastIndexOf("\n");
      if (complete < 0) return json(res, 200, { messages: [], cursor: after });
      const rows = [];
      for (const line of text.slice(0, complete).split("\n")) {
        if (!line.trim()) continue;
        try {
          rows.push(JSON.parse(line));
        } catch {
          /* torn or corrupt line: skip it, the cursor still advances */
        }
      }
      return json(res, 200, { messages: rows, cursor: after + complete + 1 });
    }
    if (req.method === "POST" && url.pathname === "/stop") {
      json(res, 200, { stopping: true });
      shutdown();
      return;
    }
    json(res, 404, { error: "unknown route" });
  } catch (e) {
    logger.error(e, "http handler failed");
    json(res, 500, { error: String((e && e.message) || e).slice(0, 300) });
  }
});

function shutdown() {
  stopping = true;
  try {
    if (sock) sock.end(undefined);
  } catch { /* already closed */ }
  try {
    if (PIDFILE) fs.unlinkSync(PIDFILE);
  } catch { /* best-effort */ }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 1500).unref();
}

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

ensureFiles();
server.listen(PORT, "127.0.0.1", () => {
  logger.info({ port: PORT }, "sidecar listening (localhost only)");
});
connect().catch((e) => {
  logger.error(e, "initial connect failed");
  // Keep the HTTP seam alive so /status can report the failure; a watcher
  // restart or re-pair attempt recreates the socket.
});
