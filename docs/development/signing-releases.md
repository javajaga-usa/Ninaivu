# Signing the downloads

The release workflow (`.github/workflows/release.yml`) signs the Mac and
Windows downloads when the repository has the secrets below, and builds them
unsigned when it does not. The release notes say which: while a build is
unsigned they tell the family how to open it anyway.

Signing is what lets a Mac keep its answers. macOS remembers "Ninaivu may read
the Documents folder" or "the drive with the library" for an app by who signed
it and what it is called. An unsigned app is a different app to macOS every
release, so every update asks again (and the Control Panel can be kept out of
its own folder until somebody allows it). A Ninaivu signed with a Developer ID
asks once, and the answer holds for every later version.

## The Mac: four secrets

| Secret | What it is |
| --- | --- |
| `MAC_SIGN_P12_BASE64` | The Developer ID Application certificate with its private key, exported as a `.p12` file and written as base64 |
| `MAC_SIGN_P12_PASSWORD` | The password the `.p12` was exported with |
| `MAC_NOTARY_APPLE_ID` | The Apple ID of the developer account (an email address) |
| `MAC_NOTARY_PASSWORD` | An app-specific password for that Apple ID, not its real password |

Two more are read from the certificate and are only needed when there is a
reason to say them: `MAC_SIGN_IDENTITY` (its full name, `Developer ID
Application: Name (TEAMID)`) and `MAC_NOTARY_TEAM_ID` (the ten characters in
the brackets).

### Making them

1. **Join the Apple Developer Program** at
   [developer.apple.com/programs](https://developer.apple.com/programs/) as an
   individual. The certificate needs the account holder's role.
2. **Make the certificate.** On the Mac, in Xcode: Settings → Accounts → the
   Apple ID → Manage Certificates → **+** → **Developer ID Application**.
   (Without Xcode: Keychain Access → Certificate Assistant → Request a
   Certificate From a Certificate Authority, saved to disk, then
   developer.apple.com → Certificates → **+** → Developer ID Application,
   upload the request, download the certificate and open it.)
3. **Export it with its key.** Keychain Access → login → My Certificates →
   "Developer ID Application: …" → File → Export Items → `.p12`, with a new
   password. That password is `MAC_SIGN_P12_PASSWORD`.
4. **Write it as base64**, in Terminal:

   ```
   base64 -i ~/Desktop/DeveloperID.p12 | pbcopy
   ```

   What is now on the clipboard is `MAC_SIGN_P12_BASE64`. Delete the `.p12`
   file afterwards; the certificate stays in the keychain.
5. **Make the app-specific password** at
   [account.apple.com](https://account.apple.com) → Sign-In and Security →
   App-Specific Passwords → **+**, named "Ninaivu notarisation". That is
   `MAC_NOTARY_PASSWORD`.
6. **Add the four** on GitHub: the repository → Settings → Secrets and
   variables → Actions → **New repository secret**, one for each name above,
   spelled exactly so.

### What the next release then does

The macOS job imports the certificate into a keychain of its own, signs every
program inside `Ninaivu.app` and then the app (hardened runtime,
`installers/macos/entitlements.plist`), sends the zip to Apple's notary
service, staples the ticket to the app and asks Gatekeeper what a Mac that
downloads it will decide. The release fails if Gatekeeper would not take it
as a notarised Developer ID app. The job's log says `signing as Developer ID
Application: …`, and the release notes stop saying that the Mac app is not
signed.

A wrong or partial set says so in the job: a `.p12` that cannot be read with
its password, a certificate that is not a Developer ID Application one, or an
Apple ID that Apple refuses are errors; a certificate without the two
notarisation secrets is signed, not notarised, with a warning.

Pull requests never see the secrets and are always built unsigned.

The certificate lasts five years. Apps signed while it was valid keep opening
after it expires (the signatures carry a time stamp); only new releases need a
new one, under the same names.

### What does not change

The bundle identifier stays `org.ninaivu.app`, so the answers macOS keeps
follow the app from version to version. Ninaivu run from a checkout (the
"Ninaivu Control Panel" app that `Setup Ninaivu.command` makes) is not the
download and is never signed: its questions about Documents are answered in
System Settings → Privacy & Security → Files and Folders.

`Ninaivu.app` never writes into itself: its Python modules are compiled when
it is built and the launcher sets `PYTHONDONTWRITEBYTECODE`, because a signed
app that changes is no longer the app Apple notarised.
`tools/check_bring_across.py` checks that on every Mac build.

## Windows: two secrets

| Secret | What it is |
| --- | --- |
| `WINDOWS_SIGN_PFX_BASE64` | A code-signing certificate with its key, as a `.pfx` file written as base64 |
| `WINDOWS_SIGN_PFX_PASSWORD` | The `.pfx` file's password |

Without them SmartScreen says "Windows protected your PC" on the first open,
and the release notes say to choose More info → Run anyway.
