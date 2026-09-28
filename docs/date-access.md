# Family app dates

In **Admin → Library → Settings → Family app date visibility**, choose a cutoff
date and a range for each role: **Before cutoff**, **On or after cutoff**, or
**All dates**. Settings are saved in the library database and apply to the next
request; refresh an open family page to redraw its albums.

The defaults are January 1, 2014, with family members and guests seeing dates on
or after the cutoff and admins seeing all dates. Existing
folder, profile, hidden-file and media-type restrictions still apply. The admin
management app remains unrestricted by date, and its role preview uses the
family app's date settings.

Calendar albums use each file's indexed creation/capture date. A named album's
date is the earliest dated file it contains, excluding files in the bin. Its
date determines whether the album is accessible; individual files must also
pass their own date and visibility checks. Changing a file date can therefore
change its named album's date. Undated files and albums are hidden from family
members and guests under either cutoff range. Admins can always reach undated
files, whatever their own range, so they can date, hide or remove them. With
**All dates**, undated files are available to that role, subject to other access
rules.

In **Admin → Folders**, each file has a **Creation date** field and **Save date
and move** button. Saving moves it within the same library into `YYYY/MM/DD`,
creating the folder when needed. Linked Live Photo video and supported XMP,
AAE and JSON sidecars move with it. A destination filename collision is refused
without overwriting either file. Restore files from the bin before editing them.

Edits set the indexed creation date to midnight UTC, preserving the original
file bytes and embedded metadata. Manual dates survive rescans. Asset IDs,
favourites, ratings, named album membership and media URLs stay intact. Cloud
index and archive destination references follow the new local path; existing
remote backup objects are retained. Calendar grouping, sorting and access
recompute from the updated dates.

API routes (admin app only, authenticated admin required):

- `GET /api/admin/date-policy`
- `POST /api/admin/date-policy` with `cutoff`, `admin`, `family`, `guest`;
  ranges are `before`, `after`, `all`.
- `PATCH /api/admin/assets/<id>/creation-date` with
  `{"creation_date": "2014-01-01"}`.

Family media responses and service-worker requests bypass caching so a date or
policy change cannot be bypassed by an old cached thumbnail. Direct media,
download, search, album and share-link requests enforce date access on the server.

## Homepage upload approval

Homepage uploads are held in private staging, outside the indexed library. The
uploader sees **Awaiting admin approval**. Admins receive a popup notification
and an **Uploads (N)** tab while the admin app is open; the queue refreshes every
10 seconds while the page is visible. Pending uploads persist across restarts.

In **Admin → Uploads**, preview the file and review its creation date, then choose
**Approve and file**. Approval places it in `YYYY/MM/DD` under the uploader's
assigned library folder (including any assigned subfolder). Camera/container
metadata is preferred; filename dates and file timestamps provide fallbacks.
The admin can correct a missing or inaccurate date before approval. Normal date,
folder and role visibility rules apply after approval.

Repeated filenames are kept with unique suffixes. Retrying an already completed
approval returns the same asset; failed moves leave the upload pending. Pending
originals and previews are accessible only in the authenticated admin app and
are excluded from library scans, search, albums and share links.
