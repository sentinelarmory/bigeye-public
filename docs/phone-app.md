# The phone app (Android)

`android/` — package `no.sentinel.bigeye`, Java, no libraries, minSdk 26, target 35.
Built offline with the Android SDK, Gradle 9.3.1 and AGP 9.1.0 already on the machine.

## What it does

- **The page.** A WebView with the same BigEye page as the browser. Everything built on the page
  shows up in the app without an app update. The page hides its "📱 Phone app" button inside the app
  (user agent tag `SentinelBigEyeApp`).
- **Alerts, only when they need you.** A foreground service keeps one long-poll open to
  `/app/alerts?after=<id>&wait=50` and notifies, with the event picture when there is one, for:
  - `urgent`: a person while BigEye is set to Away (same rule as the phone messages)
  - `action`: a camera that was live and has been gone for 10 minutes; the guard asking for help
  - `info` (phone paired, camera back) is kept in the list but does not notify
- **No cloud.** No Firebase, no push service. The phone talks only to BigEye: at home on the LAN, away
  through your own VPN. Doze would cut the line, so the app asks to be exempt from battery
  optimisation. A network change triggers a reconnect at once; otherwise it backs off from 10 s to 5 min.

## Pairing and access

1. On the PC: **📱 Phone app → New code** (8 characters, valid 10 minutes, works once).
2. In the app: BigEye's address (`[app] phone_url` in `bigeye.toml` is shown on the page) and the code. The app trades the code for its own random key
   (`/app/pair`); BigEye stores only the SHA-256 of it (`~/.config/sentinel-bigeye/app-devices.json`, mode 600).
3. The WebView logs in by POSTing the key to `/app/login`, which sets an HttpOnly cookie; the alert service
   sends the key as `X-BigEye-Key`.

The phone reaches BigEye on a separate listener (`--app-port 8772`) that refuses anything without a paired
key, apart from pairing, login and `/app/download` (the APK, which holds no secrets). Remove a phone in the
same panel and it is locked out at once. If BigEye runs inside WSL on Windows, forward the port from the LAN address with `netsh interface portproxy` and allow
it in the firewall only from your LAN and VPN subnets.

## Build

```bash
cd android && ANDROID_HOME=~/Android/Sdk ./gradlew --offline assembleRelease
```

Release signing reads `~/.config/sentinel-bigeye/android/signing.properties` (outside the repo); without it
the release build is unsigned. Copy the APK to `~/.config/sentinel-bigeye/android/sentinel-bigeye.apk`
and the phone can download it from `http://<pc>:8772/app/download`.

A test alert that looks like the real thing (marked TEST, with a current picture):
`curl localhost:8770/app/test-alert` (only from the PC itself).
