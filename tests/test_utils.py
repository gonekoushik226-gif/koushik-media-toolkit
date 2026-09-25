import os
import random
import time
from pathlib import Path

import pytest

from app.utils import filenames, sorting, timefmt, units, urls
from app.utils.logging_setup import redact


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("My Video: Part 1/2?", "My Video_ Part 1_2_"),
            ('a<b>c"d|e*f', "a_b_c_d_e_f"),
            ("   spaced    out   ", "spaced out"),
            ("trailing dots...", "trailing dots"),
            ("tab\tand\nnewline", "tab and newline"),
            ("CON", "_CON"),
            ("con.txt", "_con.txt"),
            ("LPT1", "_LPT1"),
            ("COM¹", "_COM¹"),
            ("console", "console"),
            ("", "output"),
            ("...", "output"),
            ("日本語のタイトル 🎵", "日本語のタイトル 🎵"),
            ("..\\..\\windows\\system32", ".._.._windows_system32"),
        ],
    )
    def test_cases(self, raw, expected):
        assert filenames.sanitize_filename(raw) == expected

    def test_never_contains_separators(self):
        assert "/" not in filenames.sanitize_filename("a/b\\c") and "\\" not in filenames.sanitize_filename("a/b\\c")

    def test_length_limit(self):
        result = filenames.sanitize_filename("x" * 500)
        assert len(result) == filenames.MAX_NAME_LENGTH

    def test_fallback(self):
        assert filenames.sanitize_filename("???", fallback="video") == "___"
        assert filenames.sanitize_filename("  ", fallback="video") == "video"


class TestBuildFilename:
    def test_adds_extension(self):
        assert filenames.build_filename("report", "PDF") == "report.pdf"

    def test_does_not_duplicate_extension(self):
        assert filenames.build_filename("clip.mp4", ".mp4") == "clip.mp4"

    def test_keeps_other_dots(self):
        assert filenames.build_filename("v1.2 final", "mp4") == "v1.2 final.mp4"

    def test_sanitizes(self):
        assert filenames.build_filename("a:b", "mp3") == "a_b.mp3"


def test_unique_path(tmp_path):
    first = tmp_path / "file.txt"
    assert filenames.unique_path(first) == first
    first.write_text("x")
    assert filenames.unique_path(first).name == "file (1).txt"
    (tmp_path / "file (1).txt").write_text("x")
    assert filenames.unique_path(first).name == "file (2).txt"


def test_unique_paths_avoids_clashes_within_batch(tmp_path):
    (tmp_path / "a.png").write_text("x")
    result = filenames.unique_paths([tmp_path / "a.png", tmp_path / "a.png", tmp_path / "b.png"])
    assert [p.name for p in result] == ["a (1).png", "a (2).png", "b.png"]


def test_temp_sibling_keeps_extension(tmp_path):
    tmp = filenames.temp_sibling(tmp_path / "movie.mp4")
    assert tmp.suffix == ".mp4" and tmp.parent == tmp_path and filenames.PARTIAL_MARKER in tmp.name


def test_numbered_names():
    assert filenames.numbered_names("document", 3, "pdf") == [
        "document_part01.pdf", "document_part02.pdf", "document_part03.pdf"]
    names = filenames.numbered_names("scan", 120, "png", "page", min_width=3)
    assert names[0] == "scan_page001.png" and names[-1] == "scan_page120.png"


class TestNaturalSort:
    def test_numbers_in_order(self):
        names = ["page10.png", "page2.png", "page1.png", "Page3.png"]
        assert sorted(names, key=sorting.natural_key) == ["page1.png", "page2.png", "Page3.png", "page10.png"]

    def test_mixed_text_and_numbers(self):
        names = ["img12b", "img12a", "img2", "img", "10", "9"]
        assert sorted(names, key=sorting.natural_key) == ["9", "10", "img", "img2", "img12a", "img12b"]

    def test_deterministic_ties(self):
        assert sorted(["file01", "file1"], key=sorting.natural_key) == sorted(["file1", "file01"], key=sorting.natural_key)


class TestSortPaths:
    @pytest.fixture
    def files(self, tmp_path):
        paths = []
        for index, (name, size) in enumerate([("b10.jpg", 300), ("b2.jpg", 100), ("A1.jpg", 200)]):
            path = tmp_path / name
            path.write_bytes(b"x" * size)
            stamp = time.time() - 1000 + index * 100
            os.utime(path, (stamp, stamp))
            paths.append(path)
        return paths

    def test_name(self, files):
        assert [p.name for p in sorting.sort_paths(files, sorting.SortKey.NAME)] == ["A1.jpg", "b10.jpg", "b2.jpg"]

    def test_natural(self, files):
        assert [p.name for p in sorting.sort_paths(files, sorting.SortKey.NATURAL)] == ["A1.jpg", "b2.jpg", "b10.jpg"]

    def test_modified(self, files):
        assert [p.name for p in sorting.sort_paths(files, sorting.SortKey.MODIFIED)] == ["b10.jpg", "b2.jpg", "A1.jpg"]

    def test_size_reverse(self, files):
        result = sorting.sort_paths(files, sorting.SortKey.SIZE, reverse=True)
        assert [p.name for p in result] == ["b10.jpg", "A1.jpg", "b2.jpg"]

    def test_date_taken_uses_callback_then_creation(self, files):
        taken = {"b2.jpg": 50.0}
        result = sorting.sort_paths(files, sorting.SortKey.DATE_TAKEN, date_taken=lambda p: taken.get(p.name))
        assert result[0].name == "b2.jpg"

    def test_missing_files_go_last(self, files, tmp_path):
        ghost = tmp_path / "ghost.jpg"
        result = sorting.sort_paths([ghost, *files], sorting.SortKey.SIZE)
        assert result[-1] == ghost

    def test_created_does_not_crash(self, files):
        assert len(sorting.sort_paths(files, sorting.SortKey.CREATED)) == 3


def test_shuffled_keeps_items():
    items = [Path(f"{i}.png") for i in range(20)]
    result = sorting.shuffled(items, random.Random(1))
    assert sorted(result) == sorted(items) and result != items


class TestMoveItems:
    def test_move_up_block(self):
        items, sel = sorting.move_items(list("abcde"), [2, 3], -1)
        assert items == list("acdbe") and sel == [1, 2]

    def test_move_down(self):
        items, sel = sorting.move_items(list("abc"), [0], 1)
        assert items == list("bac") and sel == [1]

    def test_edges_do_nothing(self):
        assert sorting.move_items(list("abc"), [0], -1)[0] == list("abc")
        assert sorting.move_items(list("abc"), [2], 1)[0] == list("abc")


class TestParseTime:
    @pytest.mark.parametrize(
        "text, seconds",
        [("90", 90), ("1:30", 90), ("01:02:03", 3723), ("00:00:01.5", 1.5), ("75:00", 4500),
         (" 2:05 ", 125), ("0:0:0", 0), ("1,5", 1.5)],
    )
    def test_valid(self, text, seconds):
        assert timefmt.parse_time(text) == pytest.approx(seconds)

    @pytest.mark.parametrize("text", ["", "abc", "1:60", "1:61:00", "-5", "1:2:3:4", "1.5:30", "nan", "inf"])
    def test_invalid(self, text):
        with pytest.raises(ValueError):
            timefmt.parse_time(text)


def test_format_timestamp():
    assert timefmt.format_timestamp(3723.5) == "01:02:03.500"
    assert timefmt.format_timestamp(59.9996) == "00:01:00.000"
    assert timefmt.format_timestamp(61, with_ms=False) == "00:01:01"
    assert timefmt.format_timestamp(None) == "00:00:00.000"


def test_format_duration():
    assert timefmt.format_duration(125) == "2:05"
    assert timefmt.format_duration(3723) == "1:02:03"
    assert timefmt.format_duration(None) == "-"


def test_units():
    assert units.human_size(None) == "-"
    assert units.human_size(512) == "512 bytes"
    assert units.human_size(1536) == "1.5 KB"
    assert units.human_size(5 * 1024**3) == "5.0 GB"
    assert units.human_bitrate(128.4) == "128 kbps"
    assert units.human_bitrate(8981.8) == "9.0 Mbps"
    assert units.human_bitrate(0) == "-"
    assert units.human_eta(75) == "1m 15s left"


class TestUrls:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("https://example.com/a.pdf", "https://example.com/a.pdf"),
            ("  <https://example.com/x>  ", "https://example.com/x"),
            ("www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
            ("example.org/file.pdf", "https://example.org/file.pdf"),
            ("http://127.0.0.1:8080/v.mp4", "http://127.0.0.1:8080/v.mp4"),
        ],
    )
    def test_valid(self, text, expected):
        assert urls.validate_web_url(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "ftp://example.com/file", "file:///C:/Windows/win.ini", "javascript:alert(1)",
         "https://", "https://exa mple.com", "not a url", "https://example.com:99999/x", "https://..com/"],
    )
    def test_invalid(self, text):
        with pytest.raises(urls.UrlError):
            urls.validate_web_url(text)

    def test_filename_from_url(self):
        assert urls.filename_from_url("https://x.org/docs/My%20Report.pdf?dl=1") == "My Report.pdf"
        assert urls.filename_from_url("https://x.org/") == ""

    def test_redact_url_keeps_video_id_only(self):
        redacted = urls.redact_url("https://www.youtube.com/watch?v=abc&signature=SECRET&token=T")
        assert "v=abc" in redacted and "SECRET" not in redacted and "<redacted>" in redacted

    def test_redact_url_removes_credentials(self):
        assert "pass" not in urls.redact_url("https://user:pass@example.com/a?x=1")


def test_log_redaction():
    text = redact("GET https://cdn.example.com/v.mp4?sig=abc123&expire=5 with Authorization: Bearer XYZ password=hunter2")
    assert "abc123" not in text and "XYZ" not in text and "hunter2" not in text
    assert "https://cdn.example.com/v.mp4" in text
