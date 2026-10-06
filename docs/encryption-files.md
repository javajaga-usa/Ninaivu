# Encryption file helpers

`ninaivu.cloud.crypto.encrypt_file_to_file` and `decrypt_file_to_file` process
files in 1 MiB chunks instead of loading the entire input into Python memory.
They retain their existing signatures and return the number of output bytes.
The byte-array helpers remain available for small, in-memory payloads.

## Compatibility

The V1 layout is unchanged:

`MAGIC | 16-byte salt | 12-byte nonce | ciphertext | 16-byte authentication tag`

AES-256-GCM, PBKDF2-HMAC-SHA256 with 100,000 rounds, and the authenticated magic
header are unchanged. Encryption generates a fresh random salt and nonce for
each file. Existing V1 files can be decrypted with the streaming helper; new
files can be decrypted with the existing byte helper when they fit in memory.
Tests fix randomness only to verify exact byte-for-byte compatibility.

The helpers enforce GCM's per-message plaintext limit of 64 GiB minus 32 bytes.
Supporting larger individual files would require a separately designed format;
this change does not silently split messages or alter the V1 format.

## Output and failure behavior

Output is written to a temporary file beside the destination. After successful
completion, it is flushed, synced, closed, and published with `os.replace`.
The input is closed first, allowing explicit same-path transformations on Windows.

During decryption, the final 16 bytes are retained as the authentication tag.
The destination is replaced only after `finalize_with_tag` succeeds. This follows
the [cryptography GCM guidance](https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/#cryptography.hazmat.primitives.ciphers.modes.GCM)
that decrypted data must not be used before authentication completes.

Wrong passwords, tampering, truncated input, read/write failures, and publication
failures leave an existing destination intact and remove the temporary file on
normal exception unwinding. A failed operation does not create a destination
when none existed. The destination filesystem must have room for the complete
temporary output in addition to any existing destination.

Decryption temporarily writes plaintext to disk. Use a trusted destination
directory with appropriate filesystem permissions. Hard process termination or
power loss can leave `.ninaivu-crypto-*.tmp` files behind; cleanup is not a secure
erase guarantee. These helpers require exclusive access to their input while
running and do not implement resumable encryption.

## Encrypted Drive backup (V2)

In **Admin → Cloud → Encryption**, an administrator makes an encryption key from
a passphrase and then turns on **Encrypt files before uploading**. From then on
each file is encrypted on the Ninaivu machine before it is sent; Drive holds only
`name.jpg.ninaivu` ciphertext. Files uploaded before encryption was on are not
sent again and stay as they were — the panel shows how many of each there are.

**V2 format.** `NINAIVU_ENC_V2\0 | key id (8 bytes) | nonce (12) | ciphertext |
tag (16)`, AES-256-GCM with the magic and key id as associated data. V1 derives a
key per file with PBKDF2, which at a million files is days of CPU; V2 uses one
key, derived once with scrypt (N=2^17, r=8, p=1, random 16-byte salt; keys made
earlier used N=2^15 and keep it, because the settings are stored with the key).

**The key** is stored in `cloud-encryption.json` in the state folder (owner-only
permissions where the platform has them) so uploads continue after a restart.
The passphrase is never stored. If encryption is on and the key file is missing
or damaged, uploading stops with an error rather than sending files in the clear.
The file is written under a fresh temporary name, synced to disk, and then
renamed into place. A damaged key file no longer has to be deleted by hand before
a new key can be made: it is moved aside as `cloud-encryption.damaged-<time>.json`.

**Recovery.** When the key is made the console downloads a recovery file,
`ninaivu-recovery-<key id>.json`, which contains the key; it can be downloaded
again from the panel (each download is audited). Ninaivu also uploads
`ninaivu-encryption.json` — the salt, scrypt settings and key id, not the key — to
the Drive folder, and writes it beside the off-site copy's manifest, so the
passphrase alone is enough if the recovery file is lost, with or without Drive.
A folder that already holds the settings of another key gets a second file named
for the new key, `ninaivu-encryption-<key id>.json`; the restore tries each.
Lose both the recovery file and the passphrase and the backups cannot be
decrypted.

**Importing a recovery file.** On a rebuilt machine, `POST
/api/cloud/encryption/import` with `{"recovery": <the file's contents>}` makes the
key in the recovery file this machine's key, so the Drive backups and the
off-site copy carry on with the key they were made with. A different key already
on the machine is replaced only when `"replace": true` is sent, and is kept
beside it as `cloud-encryption.replaced-<time>.json`, never deleted.

Download the Drive folder, then:

```bash
python tools/cloud_decrypt.py --recovery ninaivu-recovery-XXXX.json downloaded-folder restored-folder
python tools/cloud_decrypt.py --params ninaivu-encryption.json downloaded-folder restored-folder
```

The second asks for the passphrase. The tool never modifies the downloaded
files, never overwrites an existing output file, and reports any file that
fails authentication or was made with a different key. It needs only Python
and the `cryptography` package (`python3 -m pip install cryptography`) with a
copy of Ninaivu's source: it loads `ninaivu/cloud/crypto.py` and `keyring.py`
on their own, without the web server or image libraries. The same goes for the
off-site copy's restore, run as `python ninaivu/cli/offsite_restore.py`.

**Resumable uploads** send the same ciphertext: each encrypted file is kept in
`cloud-upload-cache/` in the state folder until Drive confirms the upload, then
removed. Up to `cloud_parallel` files (3 by default) are in progress at once, so
the cache holds that many files' worth of ciphertext, plus any kept for an
upload that was paused and will be resumed. Ciphertext left by an upload that
will not be resumed (the file deleted or set aside since) is removed at the
start of the next run. File names and
folder names in Drive are not encrypted.

A saved resumable session is only continued when it was opened for the same
kind of bytes: the uploader records, beside the session URL, whether the
session was for ciphertext and how many bytes it expects, and starts a new
session if either no longer matches what would be sent — for instance after
encryption was switched off part-way through a large video.

**What the encryption does not bind.** The associated data authenticated with
each V2 file is the magic and the key id — not the file's name, its folder, or
anything else about which photograph it is. The format is unchanged so every
existing backup stays restorable, and this is its known limit: someone who can
write to the Drive folder cannot read or alter a file's contents undetected,
but *can* swap two encrypted files' names (or put an older ciphertext of the
same path in place of a newer one), and each will still decrypt and
authenticate — as the other photograph. The restore guards against this with
checksums kept outside Drive rather than with the encryption itself:

* restoring from Ninaivu's own record, or from the copy of the index, checks
  each download against the SHA-256 of the ciphertext recorded when it was
  sent (or, for an upload that was resumed, Drive's MD5 recorded when it
  finished), so a file swapped in Drive fails its check;
* restoring on a new machine with no record at all (walking the Drive folder)
  can only check Drive's own MD5 for each file, which proves the download is
  intact but not that the file is the one its name says. Use the copy of the
  index when there is one; the restore wizard does so automatically.

A future format that binds the file's path into the associated data would
close this gap; it would need a new magic and a migration, and is not done.

## Integration and validation

Regression tests cover V1 compatibility, empty files, chunk boundaries, tampering,
wrong passwords, truncation, safe replacement, same-path round trips, size limits,
bounded reads, missing dependencies, and interrupted input. A 32 MiB round trip
asserts that peak traced Python allocations stay below 12 MiB. This measurement
does not include all native-library allocations or operating-system caches.
