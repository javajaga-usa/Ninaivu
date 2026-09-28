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
key, derived once with scrypt (N=2^15, r=8, p=1, random 16-byte salt).

**The key** is stored in `cloud-encryption.json` in the state folder (owner-only
permissions where the platform has them) so uploads continue after a restart.
The passphrase is never stored. If encryption is on and the key file is missing
or damaged, uploading stops with an error rather than sending files in the clear.

**Recovery.** When the key is made the console downloads a recovery file,
`ninaivu-recovery-<key id>.json`, which contains the key; it can be downloaded
again from the panel (each download is audited). Ninaivu also uploads
`ninaivu-encryption.json` — the salt, scrypt settings and key id, not the key — to
the Drive folder, so the passphrase alone is enough if the recovery file is
lost. Lose both the recovery file and the passphrase and the backups cannot be
decrypted. Download the Drive folder, then:

```bash
python tools/cloud_decrypt.py --recovery ninaivu-recovery-XXXX.json downloaded-folder restored-folder
python tools/cloud_decrypt.py --params ninaivu-encryption.json downloaded-folder restored-folder
```

The second asks for the passphrase. The tool never modifies the downloaded
files, never overwrites an existing output file, and reports any file that
fails authentication or was made with a different key.

**Resumable uploads** send the same ciphertext: each encrypted file is kept in
`cloud-upload-cache/` in the state folder until Drive confirms the upload, then
removed. Only one file is in progress at a time, so the cache holds at most one
file's worth of ciphertext (plus any held by a paused upload). File names and
folder names in Drive are not encrypted.

## Integration and validation

Regression tests cover V1 compatibility, empty files, chunk boundaries, tampering,
wrong passwords, truncation, safe replacement, same-path round trips, size limits,
bounded reads, missing dependencies, and interrupted input. A 32 MiB round trip
asserts that peak traced Python allocations stay below 12 MiB. This measurement
does not include all native-library allocations or operating-system caches.
