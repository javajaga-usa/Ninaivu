"""`.ts` and `.mts` are video and also TypeScript: the archive reads the bytes.

Trusting the extension archived large declaration files such as
`typescript.d.ts` as videos. The bytes decide, and both layouts of a real
transport stream are still taken: a downloaded or broadcast `.ts`, and a
camcorder's AVCHD `.MTS`, whose packets carry a 4-byte timestamp first.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ninaivu.archive import scanner                                         # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                              # noqa: E402
from test_archive_course_material import archived_names, write             # noqa: E402
from test_archive_course_material import drive as drive                    # noqa: E402,F401


def stream(packets=400, stamped=False):
    """Distinct packets, so two streams are never archived as duplicates."""
    return b"".join((os.urandom(4) if stamped else b"") + bytes([0x47]) + os.urandom(187)
                    for _ in range(packets))


def test_typescript_is_not_video_but_streams_and_camcorder_clips_are(drive):
    root, dest = drive
    code = root / "Projects" / "node_modules" / "typescript" / "lib"
    write(code / "typescript.d.ts", b"declare namespace ts {\n  const version: string;\n}\n" * 3000)
    write(code / "Grammar.d.mts", b"GLOBAL export {};\n" * 8000)        # opens with a G
    write(root / "TV" / "recording.ts", stream())
    write(root / "Camcorder" / "00012.MTS", stream(stamped=True))

    job = ArchiveJob([str(root)], str(dest))
    job.run()

    assert sorted(archived_names(dest)) == ["00012.MTS", "recording.ts"]


def test_a_text_file_that_opens_with_a_g_is_not_sniffed_as_video(tmp_path):
    notes = tmp_path / "notes"
    notes.write_bytes(b"Grocery list: rice, dal, curd\n" * 50)
    assert scanner.sniff_media(str(notes)) is None
    extensionless = tmp_path / "CLIP0001"
    extensionless.write_bytes(stream(4, stamped=True))
    assert scanner.sniff_media(str(extensionless)) == "video"
