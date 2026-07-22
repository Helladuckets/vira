# WhatsApp sidecar

A linked-device WhatsApp client (Baileys, pinned in package.json) that Vira
runs as a local child process. Receive-only v1: it never sends a message,
never marks the account online, and never pulls full history — it appends
live inbound messages to a local NDJSON inbox that the Vira server polls.

## Toolchain

Node 20+ (no Go, no headless browser). One-time install:

    cd bridge/whatsapp && npm install

`node_modules/` is git-ignored; `package-lock.json` pins the tree.

## How Vira runs it

`server/whatsapp.py` spawns (and the owner can run by hand):

    node sidecar.js --port <whatsapp_bridge_port> \
      --session-dir data/whatsapp/session \
      --inbox data/whatsapp/inbox.ndjson \
      --pidfile data/whatsapp/sidecar.pid \
      --log data/whatsapp/sidecar.log

Every path is handed on argv — the sidecar decides nothing about where
state lives. It binds 127.0.0.1 only.

- `GET /status` — `{connected, jid, needs_pair, logged_out, ...}`
- `GET /qr` — pairing QR (raw string + PNG data URL) while unlinked
- `GET /messages?after=<byte-offset>` — inbox lines past the cursor
- `POST /stop` — graceful exit

## Pairing

Phone: WhatsApp > Settings > Linked Devices > Link a Device, scan the QR
that Vira's settings sheet renders. The session persists in
`data/whatsapp/session/` (git-ignored, owner-only perms).

## Fragility notes

- **The session directory is the linked device.** Deleting
  `data/whatsapp/session/` unlinks it and forces a re-pair — same class of
  "never clean this up" as the venv/FDA grant.
- **Protocol drift.** WhatsApp revises the multi-device protocol; if the
  sidecar stops connecting, bump the pinned `baileys` version and
  re-install. The HTTP seam is version-agnostic.
- **Session expiry.** If the phone is offline for ~14 days the link dies;
  the sidecar reports `logged_out` and Vira shows "re-pair needed".
- **Unofficial client.** This rides the same protocol as WhatsApp Web but
  is not Meta-sanctioned; a small account-ban risk exists and was accepted
  by the owner (idea note, 2026-07-21). One well-behaved, receive-only
  linked device keeps the profile minimal.
