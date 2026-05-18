# Titan Android — Trusted Web Activity (TWA)

This folder wraps the **same** Titan mobile control UI (default: `https://titan-ui.arunjain-real.workers.dev`) in a minimal installable Android app using [Trusted Web Activity](https://developer.chrome.com/docs/android/trusted-web-activity/).

Work on the `**android` git branch**; do not land TWA-only experiments on `main` unless you intend to merge.

## Prerequisites

- [Android Studio](https://developer.android.com/studio) (Ladybug or newer recommended)
- JDK 17
- Chrome installed on the device (TWA uses Custom Tabs)

## 1) Digital Asset Links (required)

Android verifies that your app may “own” the URL.

1. Build a **signed** release (or run **Gradle → signingReport** on your debug keystore) and copy the **SHA-256** fingerprint of the key that will sign the Play Store build (upload key, or App Signing key if Google signs for you).
2. Edit repo file `**docs/.well-known/assetlinks.json`**:
  - Set `package_name` to match `applicationId` in `app/build.gradle.kts` (default `in.arunjain.titan.twa`).
  - Replace `sha256_cert_fingerprints` with your real fingerprint (colon-separated hex is fine).
3. **Deploy** the `docs/` site so this file is served at:
  `https://<your-ui-host>/.well-known/assetlinks.json`
   For the default Workers static UI deploy (`wrangler deploy --name titan-ui --assets ./docs`), the file must appear at that URL on **the same origin** as `twa_default_url` in `app/src/main/res/values/strings.xml`.
4. Verify with [Google’s statement list generator / tester](https://developers.google.com/digital-asset-links/tools/generator) if needed.

## 2) Open and build in Android Studio

1. **File → Open** and select the `android/twa` directory (not the whole monorepo, unless you prefer).
2. Let Gradle sync; use **Run** on a device or emulator.
3. Release: **Build → Generate Signed Bundle / APK** and choose **Android App Bundle** for Play Console.

## 3) Changing the hosted URL

Edit `app/src/main/res/values/strings.xml` → `twa_default_url`, then update `**docs/.well-known/assetlinks.json`** and redeploy the site so the **origin** matches.

## 4) Alternative: Bubblewrap

If you prefer a wizard-driven flow, install `@bubblewrap/cli` and run `bubblewrap init` against your URL; merge useful pieces back into this Gradle project or replace this folder entirely.

## Troubleshooting

- **Blank / browser fallback**: Asset Links not reachable or fingerprint/package mismatch.
- **URL bar visible**: Verification failed; fix `assetlinks.json` or signing alignment.