"""FFmpeg command helpers that do not need FFmpeg itself."""

from pathlib import Path

import pytest

from app.core.errors import InvalidInputError
from app.models.media import MediaInfo, StreamInfo
from app.services.audio import fade_filter
from app.services.ffmpeg.codecs import (
    audio_format_for_ext,
    audio_output_ext,
    copy_audio_ext,
    nearest_bitrate,
    video_encoder_args,
    video_output_ext,
)
from app.services.ffmpeg.runner import ProgressParser, build_command, explain_ffmpeg_error
from app.services.video import atempo_chain, can_concat_copy, concat_list_text


def test_progress_parser():
    parser = ProgressParser()
    assert parser.feed("frame=10") is None
    assert parser.feed("out_time_us=N/A") is None
    assert parser.feed("out_time_us=1500000") == pytest.approx(1.5)
    assert parser.feed("out_time_ms=2500000") == pytest.approx(2.5)  # FFmpeg reports microseconds here too
    parser.feed("speed=2.05x")
    parser.feed("progress=end")
    assert parser.speed == "2.05x" and parser.finished


def test_build_command_is_argument_list():
    cmd = build_command(Path("C:/ff/ffmpeg.exe"), ["-i", "in file.mp4", "out file.mp4"])
    assert cmd[0].endswith("ffmpeg.exe") and "in file.mp4" in cmd and "-progress" in cmd and "-nostdin" in cmd


@pytest.mark.parametrize("stderr, expected", [
    (["[mov,mp4] moov atom not found", "in.mp4: Invalid data found when processing input"], "incomplete"),
    (["x.mp4: No such file or directory"], "could not be found"),
    (["Stream map '0:a' matches no streams."], "needed stream"),
    (["Unknown encoder 'libx264'"], "encoder"),
    (["weird failure"], "FFmpeg could not process"),
])
def test_explain_ffmpeg_error(stderr, expected):
    assert expected.lower() in explain_ffmpeg_error(stderr).lower()


@pytest.mark.parametrize("factor, expected", [
    (1.5, "atempo=1.5"),
    (4.0, "atempo=2.0,atempo=2"),
    (0.25, "atempo=0.5,atempo=0.5"),
    (3.0, "atempo=2.0,atempo=1.5"),
])
def test_atempo_chain(factor, expected):
    assert atempo_chain(factor) == expected


def test_concat_list_escapes_quotes(tmp_path):
    path = tmp_path / "it's a clip.mp4"
    text = concat_list_text([path])
    assert text.startswith("ffconcat version 1.0")
    assert "it'\\''s a clip.mp4'" in text


def _info(codec="h264", width=320, fps=25.0, acodec="aac"):
    streams = [StreamInfo(0, "video", codec=codec, width=width, height=240, fps=fps, pix_fmt="yuv420p")]
    if acodec:
        streams.append(StreamInfo(1, "audio", codec=acodec, sample_rate=44100, channels=2))
    return MediaInfo(Path("x.mp4"), streams=streams)


def test_can_concat_copy():
    assert can_concat_copy([_info(), _info()])
    assert not can_concat_copy([_info(), _info(width=640)])
    assert not can_concat_copy([_info(), _info(fps=30)])
    assert not can_concat_copy([_info(), _info(acodec=None)])
    assert not can_concat_copy([_info(), _info(codec="hevc")])


def test_codec_tables():
    assert video_output_ext(".MOV") == "mov" and video_output_ext("wmv") == "mp4"
    assert audio_output_ext("aac") == "m4a" and audio_output_ext("wma") == "m4a"
    assert audio_format_for_ext(".mp3").codec == "libmp3lame"
    assert copy_audio_ext("pcm_s24le") == "wav" and copy_audio_ext("opus") == "opus" and copy_audio_ext("xyz") == "mka"
    assert nearest_bitrate(129_000) == 128 and nearest_bitrate(None) == 192 and nearest_bitrate(330_000) == 320
    assert "libvpx-vp9" in video_encoder_args("webm") and "libx264" in video_encoder_args("mkv")


def test_fade_filter():
    assert fade_filter(10, 2, 3) == "afade=t=in:st=0:d=2.000,afade=t=out:st=7.000:d=3.000"
    assert fade_filter(None, 1, 0) == "afade=t=in:st=0:d=1.000"
    with pytest.raises(InvalidInputError):
        fade_filter(None, 0, 2)
    with pytest.raises(InvalidInputError):
        fade_filter(3, 2, 2)
    with pytest.raises(InvalidInputError):
        fade_filter(10, 0, 0)
