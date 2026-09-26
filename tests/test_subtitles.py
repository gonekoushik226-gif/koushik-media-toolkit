"""Burn in subtitles: subtitle file handling and real FFmpeg runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.core.errors import InvalidInputError
from app.core.jobs import JobContext
from app.services import subtitles as subs
from app.services.video import VideoService

SRT = "1\n00:00:00,500 --> 00:00:01,500\nHello subtitles!\n\n2\n00:00:02,500 --> 00:00:03,500\nSecond line\n"


# ----------------------------------------------------------------------------
# Subtitle files (no FFmpeg)
# ----------------------------------------------------------------------------
def test_force_style_values():
    style = subs.SubtitleStyle()
    text = style.force_style()
    assert "FontSize=18" in text and "Alignment=2" in text and "BorderStyle=1" in text and "Encoding=-1" in text
    top_box = subs.SubtitleStyle(size="large", color="yellow", background="box", position="top").force_style()
    assert "Alignment=6" in top_box and "BorderStyle=3" in top_box and "&H0000FFFF" in top_box and "FontSize=22" in top_box
    with pytest.raises(InvalidInputError):
        subs.SubtitleStyle(size="huge").validate()


@pytest.mark.parametrize("data, encoding, expected", [
    ("Café déjà vu".encode(), "utf-8", "Café déjà vu"),
    (b"\xef\xbb\xbf" + "Grüße".encode(), "utf-8", "Grüße"),
    ("日本語の字幕".encode("utf-16"), "utf-16", "日本語の字幕"),
    ("Café crème, déjà vu, naïve à côté".encode("cp1252"), "auto", "Café crème, déjà vu, naïve à côté"),
    ("1\n00:00:01,000 --> 00:00:03,000\nПривет, как дела? Это субтитры к фильму.\n\n2\n00:00:04,000 --> "
     "00:00:06,000\nМы идём домой, уже поздно.\n".encode("cp1251"), "auto",
     "1\n00:00:01,000 --> 00:00:03,000\nПривет, как дела? Это субтитры к фильму.\n\n2\n00:00:04,000 --> "
     "00:00:06,000\nМы идём домой, уже поздно.\n"),
])
def test_decode_detects_encoding(data, encoding, expected):
    text, _ = subs.decode_subtitle(data, "auto")
    assert text == expected


@pytest.mark.parametrize("text, encoding", [
    ("Où est la bibliothèque ? Je ne sais pas, désolé.\nC’est très étrange, il était là hier.", "cp1252"),
    ("¿Dónde está la biblioteca? ¡Mañana será otro día!", "cp1252"),
    ("Grüße aus München! Das Wetter ist schön.", "cp1252"),
    ("Привет, как дела? Это субтитры к фильму.\nМы идём домой, уже поздно.", "cp1251"),
    ("مرحبا، كيف حالك؟ هذه ترجمة الفيلم.\nنحن ذاهبون إلى المنزل الآن.", "cp1256"),
    ("Dzień dobry! Jak się masz? Żółć, gęślą jaźń.\nIdziemy do domu, już późno.", "cp1250"),
    ("Καλημέρα, τι κάνεις; Αυτοί είναι υπότιτλοι.", "cp1253"),
])
def test_legacy_code_pages_are_detected(text, encoding):
    srt = f"1\n00:00:01,000 --> 00:00:03,000\n{text}\n"
    assert subs.decode_subtitle(srt.encode(encoding))[0] == srt


def test_decode_with_chosen_encoding():
    data = "Привет, как дела? Это субтитры.".encode("cp1251")
    assert subs.decode_subtitle(data, "cp1251")[0] == "Привет, как дела? Это субтитры."
    with pytest.raises(InvalidInputError):
        subs.decode_subtitle("日本語".encode("utf-16-le"), "utf-8")
    with pytest.raises(InvalidInputError):
        subs.decode_subtitle(b"x", "klingon")


def test_find_matching_subtitles(tmp_path):
    video = tmp_path / "My Movie.mp4"
    video.write_bytes(b"")
    assert subs.find_matching_subtitles(video) is None
    (tmp_path / "My Movie.en.srt").write_text(SRT, encoding="utf-8")
    assert subs.find_matching_subtitles(video).name == "My Movie.en.srt"
    (tmp_path / "My Movie.ass").write_text("x", encoding="utf-8")
    (tmp_path / "My Movie.srt").write_text(SRT, encoding="utf-8")
    assert subs.find_matching_subtitles(video).name == "My Movie.srt"  # exact name, SRT first
    (tmp_path / "Other.srt").write_text(SRT, encoding="utf-8")
    assert subs.find_matching_subtitles(tmp_path / "Other Movie.mp4") is None


def test_prepare_subtitles(tmp_path):
    source = tmp_path / "in.srt"
    text = SRT.replace("Hello subtitles!", "Café crème, déjà vu à côté.")
    source.write_bytes(text.replace("\n", "\r\n").encode("cp1252"))
    work = tmp_path / "work"
    work.mkdir()
    copy, used = subs.prepare_subtitles(source, work)
    assert used == "cp1252" and copy.name == "subtitles.srt"
    assert copy.read_text(encoding="utf-8") == text  # UTF-8, plain line ends
    assert source.read_bytes().startswith(b"1\r\n")  # the user's file is untouched
    empty = tmp_path / "empty.srt"
    empty.write_text("  \n", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="empty"):
        subs.prepare_subtitles(empty, tmp_path)
    with pytest.raises(InvalidInputError, match="not a supported"):
        subs.prepare_subtitles(tmp_path / "movie.txt", tmp_path)


# ----------------------------------------------------------------------------
# Burning in (real FFmpeg)
# ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def plain_video(media_tools, tmp_path_factory) -> Path:
    """4 s, 640x360 plain grey picture + tone: subtitles are easy to detect."""
    out = tmp_path_factory.mktemp("subs") / "plain.mp4"
    subprocess.run([str(media_tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=gray:size=640x360:rate=25:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


def text_pixels(tools, video: Path, second: float, band: str = "bottom") -> int:
    """How many clearly bright or dark pixels (letters / outline) are in the top or bottom band."""
    frame = video.with_name(f"{video.stem}-{second:.2f}-{band}.png")
    subprocess.run([str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{second}",
                    "-i", str(video), "-frames:v", "1", str(frame)], check=True)
    with Image.open(frame) as image:
        gray = image.convert("L")
        height = gray.height
        box = (0, int(height * 0.72), gray.width, height) if band == "bottom" else (0, 0, gray.width, int(height * 0.28))
        histogram = gray.crop(box).histogram()
        return sum(histogram[221:]) + sum(histogram[:40])


def burn(media_tools, source, subtitle, output, **kwargs):
    style = kwargs.pop("style", subs.SubtitleStyle())
    return VideoService(media_tools).burn_subtitles(source, subtitle, output, style, JobContext(), **kwargs)



@pytest.mark.ffmpeg
def test_burn_srt(media_tools, plain_video, tmp_path):
    subtitle = tmp_path / "plain.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    output = tmp_path / "plain_subtitled.mp4"
    result = burn(media_tools, plain_video, subtitle, output)
    assert output.exists() and "2 subtitle(s)" in result.message
    from app.services.ffmpeg.probe import probe

    info = probe(media_tools.ffprobe, output)
    assert info.has_audio and abs(info.best_duration - 4.0) < 0.3
    assert text_pixels(media_tools, output, 1.0) > 300  # subtitle shown
    assert text_pixels(media_tools, output, 2.0) < 20  # between subtitles: nothing
    assert text_pixels(media_tools, output, 3.0) > 300
    assert text_pixels(media_tools, output, 1.0, "top") < 20


@pytest.mark.ffmpeg
def test_burn_top_position_and_box(media_tools, plain_video, tmp_path):
    subtitle = tmp_path / "top.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    output = tmp_path / "top.mp4"
    burn(media_tools, plain_video, subtitle, output,
         style=subs.SubtitleStyle(position="top", background="box", size="large", color="yellow"))
    assert text_pixels(media_tools, output, 1.0, "top") > 300
    assert text_pixels(media_tools, output, 1.0, "bottom") < 20


@pytest.mark.ffmpeg
def test_timing_adjustment(media_tools, plain_video, tmp_path):
    subtitle = tmp_path / "late.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    output = tmp_path / "shifted.mp4"
    burn(media_tools, plain_video, subtitle, output, offset=1.0)  # 0.5-1.5 -> 1.5-2.5
    assert text_pixels(media_tools, output, 1.0) < 20
    assert text_pixels(media_tools, output, 2.0) > 300
    from app.services.ffmpeg.probe import probe

    assert abs(probe(media_tools.ffprobe, output).best_duration - 4.0) < 0.3


@pytest.mark.ffmpeg
def test_ass_vtt_unicode_and_awkward_paths(media_tools, plain_video, tmp_path):
    folder = tmp_path / "O'Brien, [subs]; 50% ü 字幕"
    folder.mkdir()
    ass = folder / "movie's subs, v2.ass"
    ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n[V4+ Styles]\n"
                   "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
                   "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                   "Alignment, MarginL, MarginR, MarginV, Encoding\n"
                   "Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,"
                   "10,10,20,1\n\n[Events]\n"
                   "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                   "Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,తెలుగు · हिन्दी · 日本語\n", encoding="utf-8")
    output = folder / "out [final].mp4"
    burn(media_tools, plain_video, ass, output)  # keeps the file's own style: top of the picture (alignment 8)
    assert text_pixels(media_tools, output, 1.0, "top") > 300
    vtt = folder / "movie.vtt"
    vtt.write_text("WEBVTT\n\n00:00.500 --> 00:01.500\nWeb subtitles\n", encoding="utf-8")
    output2 = folder / "vtt.mp4"
    burn(media_tools, plain_video, vtt, output2)
    assert text_pixels(media_tools, output2, 1.0) > 300


@pytest.mark.ffmpeg
def test_problems_are_reported(media_tools, plain_video, tmp_path):
    garbage = tmp_path / "garbage.srt"
    garbage.write_text("this is not a subtitle file at all", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="No subtitles were found"):
        burn(media_tools, plain_video, garbage, tmp_path / "a.mp4")
    late = tmp_path / "late.srt"
    late.write_text("1\n01:00:00,000 --> 01:00:02,000\nToo late\n", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="after the end of the video"):
        burn(media_tools, plain_video, late, tmp_path / "b.mp4")
    subtitle = tmp_path / "ok.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    with pytest.raises(InvalidInputError, match="before the start"):
        burn(media_tools, plain_video, subtitle, tmp_path / "c.mp4", offset=-10)
    assert not list(tmp_path.glob("*.mp4"))


@pytest.mark.ffmpeg
def test_partly_outside_warns(media_tools, plain_video, tmp_path):
    subtitle = tmp_path / "long.srt"
    subtitle.write_text(SRT + "\n3\n00:01:00,000 --> 00:01:02,000\nAfter the end\n", encoding="utf-8")
    result = burn(media_tools, plain_video, subtitle, tmp_path / "w.mp4")
    assert "2 subtitle(s)" in result.message and result.warnings and "1 subtitle(s)" in result.warnings[0]


@pytest.mark.ffmpeg
def test_cancel_leaves_nothing(media_tools, plain_video, tmp_path):
    from app.core.errors import JobCancelled

    subtitle = tmp_path / "c.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    ctx = JobContext()
    ctx.cancel()
    with pytest.raises(JobCancelled):
        VideoService(media_tools).burn_subtitles(plain_video, subtitle, tmp_path / "x.mp4", subs.SubtitleStyle(), ctx)
    assert not list(tmp_path.glob("*.mp4")) and not list(tmp_path.glob("*mt-partial*"))
