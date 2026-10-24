"""Will it fit? The check before a copy onto another kind of disk.

Only reads: the index for the files, the system for the kind of disk. These
check each rule against each kind of disk, the counts and the suggestions,
the console's answer and ``ninaivu fit``.
"""

import pytest

from conftest import FAMILY, GUEST, login
from ninaivu.storage import fit

GB = 1024 ** 3


def problems(report):
    return {(i["path"], i["problem"]) for i in report["listed"]}


@pytest.mark.parametrize("fstype, family", [
    ("exfat", "exfat"), ("vfat", "fat"), ("msdos", "fat"), ("ntfs", "ntfs"),
    ("fuseblk", "ntfs"), ("smbfs", "smb"), ("cifs", "smb"), ("apfs", "apfs"),
    ("hfs", "hfs"), ("ext4", "unix"), ("btrfs", "unix"), ("", "unknown"),
    (None, "unknown"), ("something-new", "unknown"),
])
def test_each_kind_of_disk_has_its_rules(fstype, family):
    assert fit.family_of(fstype).name == family


def test_ext4_keeps_everything_a_mac_can_name():
    entries = [("2024/10/31/Diwali: day 2.jpg", 10), ("2024/10/31/what?.jpg", 10),
               ("2024/10/31/IMG_1.JPG", 10), ("2024/10/31/img_1.jpg", 10),
               ("2024/10/31/folder./a.jpg", 10)]
    report = fit.check(entries, "ext4")
    assert report["fits"] is True
    assert report["problems"] == {}


def test_windows_disks_refuse_windows_names():
    entries = [("2024/10/31/Diwali: day 2.jpg", 10), ("2024/10/31/what?.jpg", 10),
               ("2024/odd./a.jpg", 10), ("2024/con.jpg", 10), ("2024/fine.jpg", 10)]
    for fstype in ("exfat", "ntfs", "vfat", "smbfs"):
        report = fit.check(entries, fstype)
        assert report["fits"] is False
        assert problems(report) >= {
            ("2024/10/31/Diwali: day 2.jpg", "forbidden_character"),
            ("2024/10/31/what?.jpg", "forbidden_character"),
            ("2024/odd./a.jpg", "trailing_dot_or_space"),
            ("2024/con.jpg", "reserved_name"),
        }
        assert ("2024/fine.jpg", "forbidden_character") not in problems(report)


def test_a_suggestion_is_a_name_the_disk_would_take():
    report = fit.check([("2024/10/31/Diwali: day 2.jpg", 10), ("2024/con.jpg", 10)], "exfat")
    suggestions = {i["path"]: i["suggestion"] for i in report["listed"]}
    assert suggestions["2024/10/31/Diwali: day 2.jpg"] == "2024/10/31/Diwali day 2.jpg"
    assert suggestions["2024/con.jpg"] == "2024/_con.jpg"
    again = fit.check([(s, 10) for s in suggestions.values()], "exfat")
    assert again["fits"] is True


def test_names_that_differ_only_in_case_clash_where_case_is_ignored():
    entries = [("2024/IMG_1.JPG", 10), ("2024/img_1.jpg", 10), ("2024/other.jpg", 10)]
    for fstype in ("apfs", "exfat", "ntfs", "hfs"):
        report = fit.check(entries, fstype)
        assert report["problems"] == {"same_name_different_case": 2}
        clash = {i["path"]: i["clashes_with"] for i in report["listed"]}
        assert clash["2024/IMG_1.JPG"] == ["2024/img_1.jpg"]
    assert fit.check(entries, "ext4")["fits"] is True


def test_the_two_unicode_spellings_of_a_name_clash_where_they_are_one_name():
    composed = "2024/Café.jpg"                       # é as one character
    decomposed = "2024/Café.jpg"                # e and a combining accent
    entries = [(composed, 10), (decomposed, 10)]
    report = fit.check(entries, "apfs")
    assert report["problems"] == {"same_name_different_spelling": 2}
    assert fit.check(entries, "ext4")["fits"] is True
    # NTFS keeps both spellings as two names.
    assert "same_name_different_spelling" not in fit.check(entries, "ntfs")["problems"]


def test_fat32_cannot_hold_a_file_over_four_gigabytes():
    entries = [("video/wedding.mov", 6 * GB), ("video/clip.mov", 3 * GB)]
    report = fit.check(entries, "vfat")
    assert report["problems"] == {"too_large": 1}
    assert report["listed"][0]["path"] == "video/wedding.mov"
    assert report["coarse_times"] is True
    assert fit.check(entries, "exfat")["fits"] is True


def test_a_name_too_long_is_counted_in_the_disks_own_units():
    tamil = "நாள்" * 30 + ".jpg"            # 124 characters, 364 bytes in UTF-8
    entries = [(f"2024/{tamil}", 10)]
    assert fit.check(entries, "ext4")["problems"] == {"name_too_long": 1}
    assert fit.check(entries, "ntfs")["fits"] is True
    long_ascii = "a" * 260 + ".jpg"
    assert fit.check([(long_ascii, 1)], "exfat")["problems"] == {"name_too_long": 1}


def test_an_unknown_disk_gets_the_strict_rules_but_no_size_limit():
    entries = [("a:b.jpg", 1), ("big.mov", 6 * GB)]
    report = fit.check(entries, None)
    assert report["family"] == "unknown"
    assert report["problems"] == {"forbidden_character": 1}


def test_the_list_is_capped_but_the_counts_are_whole():
    entries = [(f"2024/bad{i}?.jpg", 1) for i in range(50)]
    report = fit.check(entries, "exfat", limit=10)
    assert len(report["listed"]) == 10
    assert report["problems"] == {"forbidden_character": 50}
    assert report["complete"] is False


def test_the_kind_of_disk_is_found_for_a_folder_not_made_yet(tmp_path):
    here = fit.filesystem_of(tmp_path)
    assert fit.filesystem_of(tmp_path / "not" / "made" / "yet") == here


# --- the library ---------------------------------------------------------------

def test_the_library_is_checked_from_the_index(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    one = conn.execute("SELECT id FROM assets WHERE root=? LIMIT 1", (root,)).fetchone()[0]
    conn.execute("UPDATE assets SET rel_path='2024/odd: name.jpg' WHERE id=?", (one,))
    conn.commit()
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                         (root,)).fetchone()[0]
    report = fit.check_library(conn, [root], "/somewhere", fstype="exfat")
    assert report["files"] == files
    assert ("2024/odd: name.jpg", "forbidden_character") in problems(report)
    assert fit.check_library(conn, [root], "/somewhere", fstype="ext4")["fits"] is True


@pytest.mark.parametrize("who", ["family", "guest"])
def test_only_an_admin_can_ask(app, people, who):
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    assert client.get("/api/admin/fit?target=/tmp").status_code in (401, 403)


def test_the_console_needs_a_folder(as_admin):
    response = as_admin.get("/api/admin/fit")
    assert response.status_code == 400


def test_the_console_gets_the_report(as_admin, tmp_path):
    body = as_admin.get(f"/api/admin/fit?target={tmp_path}").get_json()
    assert body["target"] == str(tmp_path)
    assert body["files"] > 0
    assert set(body) >= {"filesystem", "family", "problems", "listed", "fits"}


def test_the_command_prints_and_says_whether_it_fits(scanned, capsys, tmp_path):
    from ninaivu.cli import fit as cli
    cfg, _, _ = scanned
    data = cli.report(cfg, str(tmp_path))
    cli.print_report(data)
    out = capsys.readouterr().out
    assert f"to {tmp_path}" in out
