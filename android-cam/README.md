# BigEye Cam

Turns an old Android phone (Android 5.0 or newer) into a BigEye camera. No account, no cloud, no ads: the phone
only answers on the network it is on (put it on BigEye's camera Wi-Fi, which has no way out).

| Address | What |
|---|---|
| `http://<phone>:8080/cam.mjpg` | live picture (MJPEG), what BigEye reads |
| `/snapshot.jpg` | the newest picture |
| `/status` | JSON: battery %, charging, battery temperature, size, fps, uptime |
| `/torch?on=1` / `?on=0` | the phone's light |
| `/config?size=1280x720&fps=10&quality=70&camera=0` | settings, kept on the phone (camera 1 = front) |

Build: `ANDROID_HOME=~/Android/Sdk ./gradlew assembleRelease` (signing from
`~/.config/sentinel-bigeye/android/signing.properties`, never in the repo). Install with `adb install`, open the app
once (it asks for the camera), then turn the screen off; it keeps filming and starts again after a restart
(Android 14+: tap the "BigEye Cam stopped" notice once after an update or a restart).

Lessons from the first two phones (2026-11-16):

- Let the **camera machine** fetch the phone's picture and pass it on (one Wi-Fi hop). A main PC pulling it over the
  same camera Wi-Fi sends every picture through the air twice and starved the other cameras.
- Use a **wall charger**. A PC's USB port gives ~0.5 A; an old phone then drains, and at a few per cent it switches
  off processor cores and the picture stutters.
- Weak old phones: 800x480 at 6-8 fps. Remove the preinstalled apps you do not need (ADB) for memory.
- Samsung phones: turn on "Protect battery" (charge to 85 %) if they have it; they stay on the charger all the time.
