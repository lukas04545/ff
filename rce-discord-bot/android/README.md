# RCE Panel – Android app

The control panel as a real Android app (APK). One app for every server: it asks for the panel's address on the first
start (or gets it from the panel's **📲 Android app → Add this panel to the app** link, `rcepanel://add?url=…`), keeps a
list of servers (switch in the menu ⋮) and shows the panel fullscreen. It remembers the login, supports file uploads
(logos, pictures) and downloads, pull-to-refresh and the back button; links to other sites open in the browser.

The built app is `dist/rce-panel.apk` – every bot serves it at `/app/rce-panel.apk`, the shop at the same address.

## Building

Needs JDK 17+, Gradle 8.13+ and the Android SDK (platform 35, build-tools 35):

```bash
cd android
echo "sdk.dir=/path/to/android-sdk" > local.properties
gradle assembleRelease
cp app/build/outputs/apk/release/app-release.apk dist/rce-panel.apk
```

Raise `versionCode` / `versionName` in `app/build.gradle` for every new version.

## Signing key

The key that signs the app is **not** in the repository. Build with it like this:

```bash
RCE_APK_KEYSTORE=/path/to/rce-panel-release.keystore RCE_APK_PASSWORD='…' gradle assembleRelease
```

(alias `rcepanel`). **Keep the key file and its password safe** – phones only install an update over the old version
when it's signed with the same key. Without `RCE_APK_KEYSTORE` the release build is unsigned (not installable).
For the Play Store, use Play App Signing.

## Not supported in the app

Passkey login (Android's WebView can't do it) – log in with your password, or use the panel in Chrome for passkeys.
