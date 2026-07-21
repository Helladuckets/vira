# Vira Companion (Android)

The phone that is not an iPhone still reaches Vira. This app pairs an
Android phone with a Vira hub over Tailscale and gives the hub three
things iPhones get natively on a Mac:

- **SMS history + live SMS** — read from the phone's message store,
  uploaded in batches; new texts stream as they arrive.
- **RCS and WhatsApp capture** — those apps expose no message store, so
  incoming messages are captured from their notifications
  (sender, text, timestamp). Notification access is scoped to messaging
  apps only; nothing else is looked at.
- **Pings** — Vira's notifications (important email, renewals, finished
  jobs) long-poll from the hub and land as normal Android
  notifications. No Google push, no third-party relay.

Everything goes to the paired hub and nowhere else. Message content
lives on the hub machine (`data/companion.sqlite`), the same privacy
boundary as everything else in Vira.

## Pairing

1. On the hub: open Vira, Phone Link window, "Pair a phone".
2. In this app: "Scan or paste the pairing code", scan the QR.
3. The QR carries the hub URL and a device token minted through the
   hub's secrets store; the app authenticates every request with it.
   Unpair from either side to revoke.

The phone must reach the hub: install Tailscale on the phone and share
the hub's tailnet (or be on the same Wi-Fi).

## Build

```
cd android
./gradlew :app:assembleDebug        # apk at app/build/outputs/apk/debug/
./gradlew :app:testDebugUnitTest    # JVM unit tests (protocol, rules, queue)
```

Requires JDK 17 and the Android SDK (compileSdk 34). CI builds both on
every push that touches `android/` (.github/workflows/android.yml).

## Design notes

- Kotlin, classic Views built in code, no AppCompat/Compose — the app
  is a stack of cards and earns no framework weight.
- Dependencies: CameraX + zxing-core (QR pairing), androidx.activity
  (the lifecycle CameraX binds to). Networking is HttpURLConnection;
  JSON is org.json; storage is SharedPreferences + a JSONL queue file.
- The upload queue is durable: captures append to
  `files/upload-queue.jsonl` and leave only after the hub confirms the
  batch, so a dead network drops nothing.
- The Messages app posts notifications for both SMS and RCS. SMS
  already arrives through the provider (with the real phone number), so
  a notification whose body matches a recent provider row is skipped as
  an echo; the rest are RCS and captured (NotifRules.isSmsEcho).
- Plain HTTP is deliberate: the transport is the tailnet (WireGuard
  encryption end to end). Android's cleartext block is lifted in
  `network_security_config.xml` with the reasoning inline.

## What this is not (yet)

- No message sending from the hub through the phone.
- No WhatsApp/RCS history — Android offers no door; capture starts when
  the listener is enabled.
- The device token sits in app-private SharedPreferences, unencrypted.
  It only unlocks a hub reachable over the phone's own tailnet;
  unpairing revokes it. Keystore-wrapping it is a later hardening pass.
