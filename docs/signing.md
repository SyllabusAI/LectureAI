# Developer ID signing, notarization, and Sparkle

`release.yml` already knows how to sign, notarize, staple, and verify
Syllabus.app and its disk image, and how to publish a Sparkle appcast. All of
it is switched off until the repository has the secrets below. With none of
them a release builds exactly as it always has: signed ad hoc, a
"Unsigned build" notice in the run's summary, and release notes that carry the
Gatekeeper steps.

Turning it on is: do the Apple steps once, add the secrets, run the workflow
by hand to check, then tag.

## The secrets

Repository secrets on `SyllabusAI/LectureAI` (Settings, Secrets and
variables, Actions), all names exact:

| Secret | What it holds | Needed for |
| --- | --- | --- |
| `MACOS_CERT_P12` | The Developer ID Application certificate and its private key, exported as a .p12, **base64 encoded** | signing |
| `MACOS_CERT_PASSWORD` | The password chosen when exporting that .p12 | signing |
| `ASC_API_KEY_ID` | The App Store Connect API key's Key ID (10 characters) | notarization |
| `ASC_API_ISSUER_ID` | The Issuer ID shown above the keys list (a UUID) | notarization |
| `ASC_API_KEY_P8` | The full text of `AuthKey_<KEYID>.p8`, including the BEGIN and END lines | notarization |
| `SPARKLE_ED_PRIVATE_KEY` | Sparkle's EdDSA private key, as `generate_keys -x` exports it (one base64 line) | self-update |

Optional repository **variable** (not secret): `SYLLABUS_CODESIGN_IDENTITY`,
the identity's full name or SHA-1, only if the .p12 ever holds more than one
Developer ID Application identity. Without it the workflow uses the one it
finds.

The five Apple secrets go together: some but not all of them fails the run
on purpose, so a mistyped name cannot quietly publish an unsigned release.
`SPARKLE_ED_PRIVATE_KEY` without the Apple five also fails: Sparkle updates
need a signed, notarized app. The Sparkle public key is not a secret and is
not stored anywhere; the workflow derives it from the private key and writes
it into Info.plist, so the two cannot disagree.

## One-time steps on the Apple side (Trace)

Developer ID certificates can only be created by the **Account Holder** of
the Apple Developer team, so these are Trace's. Enrollment has to be complete
first (the team shows in developer.apple.com/account with a Team ID).

### 1. The Developer ID Application certificate

1. On a Mac, open Keychain Access. From the menu: Keychain Access,
   Certificate Assistant, Request a Certificate From a Certificate Authority.
   Enter your email, choose "Saved to disk", and save the `.certSigningRequest`.
2. Go to developer.apple.com/account/resources/certificates, click **+**,
   choose **Developer ID Application**, and pick the **G2 Sub-CA** profile.
   Upload the request file, then download the `.cer`.
3. Double-click the `.cer`. It lands in the login keychain next to the
   private key the request made.
4. In Keychain Access, My Certificates, find
   "Developer ID Application: Main Course Media LLC (TEAMID)". Expand it to
   check the private key is under it, select the certificate, File, Export
   Items, format **Personal Information Exchange (.p12)**, and set a strong
   password.
5. Encode it and set the two secrets (from any Mac with `gh` signed in):

   ```sh
   base64 -i DeveloperID.p12 | gh secret set MACOS_CERT_P12 --repo SyllabusAI/LectureAI
   gh secret set MACOS_CERT_PASSWORD --repo SyllabusAI/LectureAI   # paste the password
   ```

6. Keep the .p12 and its password in the password manager, then delete the
   loose file. Apple allows only a few Developer ID certificates per team;
   do not make a new one per machine.

### 2. The App Store Connect API key, for notarytool

1. In App Store Connect: Users and Access, Integrations, App Store Connect
   API. The first time, the Account Holder has to click **Request Access**
   and accept the terms.
2. Under **Team Keys**, click **+**, name it `Syllabus notarization`, and give
   it the **Developer** role (enough for notarization, nothing more).
3. Download the `.p8`. Apple offers the download **once**. Note the Key ID
   in the key's row and the Issuer ID above the table.
4. Set the three secrets:

   ```sh
   gh secret set ASC_API_KEY_ID    --repo SyllabusAI/LectureAI   # paste the Key ID
   gh secret set ASC_API_ISSUER_ID --repo SyllabusAI/LectureAI   # paste the Issuer ID
   gh secret set ASC_API_KEY_P8    --repo SyllabusAI/LectureAI < AuthKey_XXXXXXXXXX.p8
   ```

5. Keep the .p8 in the password manager and delete the loose file.

## One-time step for Sparkle (Trace or Liam)

Sparkle signs each update with an EdDSA key of our own, separate from Apple's
certificate. Every installed copy trusts the public half forever, so **losing
the private key means installed copies can never be sent another update**;
they would have to download one by hand. Back it up before anything else.

1. Download Sparkle 2.10.0 (the version pinned in `packaging/sparkle/release`)
   from github.com/sparkle-project/Sparkle/releases and unpack it.
2. `./bin/generate_keys` makes the key, stores it in the login keychain, and
   prints the public key. Nothing needs the printed value; the workflow
   derives it.
3. `./bin/generate_keys -x sparkle_private_key.txt` exports the private key.
4. Store that file's contents in the password manager, then:

   ```sh
   gh secret set SPARKLE_ED_PRIVATE_KEY --repo SyllabusAI/LectureAI < sparkle_private_key.txt
   rm sparkle_private_key.txt
   ```

Adding this secret is what turns self-update on. Leave it off to ship signed
builds that still only use the GitHub notice.

## Checking a signed build before a real release

1. Run the release workflow by hand on `main` (Actions, Release Syllabus.app,
   Run workflow). A manual run builds everything and publishes nothing.
2. In the run: the summary shows "Signed build" (and "Sparkle" if on), and
   the steps "Sign Syllabus.app with Developer ID", both notarize steps, and
   "Gatekeeper accepts the app and the image" are green. A rejected
   notarization prints Apple's log naming each file it objected to.
3. Download the artifact and check it on a Mac:

   ```sh
   spctl --assess --type open --context context:primary-signature -vv Syllabus-X.Y.Z.dmg
   #   accepted, source=Notarized Developer ID
   xcrun stapler validate Syllabus-X.Y.Z.dmg
   hdiutil attach Syllabus-X.Y.Z.dmg
   spctl --assess --type execute -vv /Volumes/Syllabus/Syllabus.app
   codesign -dv --verbose=4 /Volumes/Syllabus/Syllabus.app      # Authority=Developer ID Application: ...
   codesign -d --entitlements - /Volumes/Syllabus/Syllabus.app  # the four in packaging/entitlements.plist
   plutil -p /Volumes/Syllabus/Syllabus.app/Contents/Info.plist | grep SU   # only with Sparkle on
   ```

4. The real test: send the image to another Mac through a browser download
   (so it is quarantined), open it, drag it to Applications, and open it. It
   should open with the ordinary "downloaded from the internet" question and
   no "could not verify" window. Record a short lecture to confirm the
   microphone prompt appears and recording works under the hardened runtime.

Then tag as usual. The release notes drop the Gatekeeper paragraph on their
own for a signed build (the line containing "not yet signed with an Apple
Developer ID" in `packaging/release-notes.md`).

## What happens on each signed release

1. `build.sh` builds the app; with Sparkle on it also fetches the pinned
   Sparkle framework (checked against its sha256) and embeds it, and the
   spec writes `SUFeedURL` and `SUPublicEDKey` into Info.plist.
2. The certificate is imported into a throwaway keychain.
3. `packaging/sign.sh` signs inside out with the hardened runtime and a
   secure timestamp: every library and extension module, then the
   executables (Syllabus, ffmpeg, ffprobe) with `packaging/entitlements.plist`,
   then Python.framework, then Sparkle's helpers as Sparkle documents, then
   the app. It ends with `codesign --verify --deep --strict`.
4. `packaging/notarize.sh` notarizes the app and staples it.
5. `dmg.sh` wraps it; the image is signed, notarized, stapled, and its
   `.sha256` rewritten (stapling changes the file).
6. `spctl` must accept both, or nothing is published.
7. With Sparkle on, `sign_update` signs the final image and
   `packaging/appcast.py` writes `appcast.xml`, attached to the release. The
   app's feed is `releases/latest/download/appcast.xml`, which is why
   `ffmpeg.yml` publishes its builds with `--latest=false`.

## Things to know when turning it on

- **The first signed version asks for the microphone again**, once. macOS
  ties the permission to the app's signature, and ad hoc to Developer ID is a
  new identity. Say so in that release's notes.
- **Sparkle starts with the first release that carries it.** Copies already
  installed are unsigned and have no Sparkle; they get that one release the
  old way (the in-app notice links to the download), and update themselves
  after that.
- **Sparkle asks before installing** (`SUAutomaticallyUpdate` is off) and
  checks daily, like the GitHub check. When someone chooses Install and
  Relaunch, the app quits and is replaced. A recording keeps running on its
  own after the app quits, from the old copy's files, so an update in the
  middle of a recording is a risk worth testing before launch; the safe
  habit until then is to install updates between lectures.
- **The download page's Gatekeeper walkthrough** (`/syllabus/download`,
  main-course-media) can be cut once a signed release is live; its header
  comment says what to keep.
- **Local signed builds**: with the certificate in your login keychain,
  `SYLLABUS_CODESIGN_IDENTITY="Developer ID Application: ..." packaging/build.sh`
  signs during the build, or run `packaging/sign.sh dist/Syllabus.app "<identity>"`
  afterward. `packaging/sign.sh <app> -` signs ad hoc with the hardened
  runtime; such a build will not launch (library validation needs a Team ID),
  which is expected and says nothing about a real signature.
- **Entitlements** are in `packaging/entitlements.plist`, each with its
  reason. If a signed build crashes where an unsigned one did not, the
  hardened runtime is the first suspect: `log stream --predicate
  'subsystem == "com.apple.securityd"'` while reproducing, and compare with
  that file.
