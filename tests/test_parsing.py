"""Parsing of ffprobe output, yt-dlp format lists and download planning."""

import json
from pathlib import Path

import pytest

from app.core.errors import DownloadError, InvalidInputError
from app.models.media import media_summary, parse_ffprobe_json, parse_fraction
from app.models.remote import (
    FormatKind,
    codec_family,
    friendly_codec,
    parse_format,
    parse_info_dict,
    recommended_formats,
)
from app.services.downloads.plans import (
    choose_container,
    extracted_audio_ext,
    pick_audio_for_video,
    plan_audio_download,
    plan_video_download,
)
from app.services.downloads.ytdlp import (
    clean_ytdlp_message,
    explain_ytdlp_error,
    media_from_info,
    ytdlp_version_age_days,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def youtube():
    return parse_info_dict(load("ytdlp_youtube_sample.json"), "https://www.youtube.com/watch?v=aqz-KE-bpKQ")


# ----------------------------------------------------------------------------
# ffprobe
# ----------------------------------------------------------------------------
class TestFfprobeParsing:
    def test_phone_video(self):
        info = parse_ffprobe_json(load("ffprobe_phone_video.json"), "IMG_0001.MOV")
        video, audio = info.primary_video, info.primary_audio
        assert info.duration == pytest.approx(12.345)
        assert info.size == 23456789
        assert video.codec == "hevc" and video.fps == pytest.approx(29.97, rel=1e-3)
        assert video.rotation == 90
        assert video.display_size == (1080, 1920)  # portrait video shot on a phone
        assert audio.sample_rate == 48000 and audio.channels == 2 and audio.language == "eng"
        assert len(info.streams) == 3 and not info.has_cover_art

    def test_cover_art_is_not_video(self):
        info = parse_ffprobe_json(load("ffprobe_mp3_cover.json"), "song.mp3")
        assert info.has_audio and not info.has_video and info.has_cover_art
        assert info.tags["title"] == "Song" and info.tags["artist"] == "Band"  # keys lower-cased

    def test_summary_mentions_streams(self):
        text = media_summary(parse_ffprobe_json(load("ffprobe_phone_video.json"), "IMG_0001.MOV"))
        assert "HEVC 1920x1080" in text and "rotated 90" in text and "AAC" in text

    def test_empty_and_broken_values(self):
        info = parse_ffprobe_json({"streams": [{"codec_type": "video", "width": "N/A", "avg_frame_rate": "0/0"}],
                                   "format": {"duration": "N/A"}}, "x.mp4")
        assert info.duration is None and info.primary_video.width is None and info.primary_video.fps is None

    @pytest.mark.parametrize("value, expected", [("30000/1001", 29.97), ("25", 25.0), ("0/0", None), ("N/A", None),
                                                 ("1/0", None), (None, None), ("abc", None)])
    def test_parse_fraction(self, value, expected):
        result = parse_fraction(value)
        assert (result is None and expected is None) or result == pytest.approx(expected, rel=1e-3)


# ----------------------------------------------------------------------------
# yt-dlp formats
# ----------------------------------------------------------------------------
class TestFormatParsing:
    def test_storyboards_are_dropped(self, youtube):
        assert youtube.get("sb0") is None

    def test_kinds(self, youtube):
        assert youtube.get("251").kind is FormatKind.AUDIO_ONLY
        assert youtube.get("299").kind is FormatKind.VIDEO_ONLY
        assert youtube.get("18").kind is FormatKind.COMBINED
        assert youtube.get("233").kind is FormatKind.AUDIO_ONLY  # codec unknown, still audio only

    def test_values(self, youtube):
        fmt = youtube.get("299")
        assert (fmt.width, fmt.height, fmt.fps, fmt.ext) == (1920, 1080, 60, "mp4")
        assert fmt.size_bytes == 257619653 and not fmt.size_is_estimate
        assert youtube.get("18").size_is_estimate
        assert youtube.get("269").size_bytes is None  # never invented

    def test_default_selection(self, youtube):
        assert youtube.default_format_ids == ["401", "251"]
        assert youtube.default_audio.format_id == "251"

    def test_sorting(self, youtube):
        assert youtube.video_formats[0].format_id == "401"
        assert youtube.audio_formats[0].format_id == "140"  # highest bitrate
        assert youtube.audio_formats[-1].format_id == "140-drc"  # compressed-dynamics variant ranks last

    def test_recommended_hides_duplicates(self, youtube):
        ids = {f.format_id for f in recommended_formats(youtube.formats)}
        assert "140-drc" not in ids  # DRC duplicate
        assert "269" not in ids  # HLS duplicate of 160
        assert "233" not in ids  # HLS audio while direct audio exists
        assert {"140", "251", "160", "299", "303", "401", "18"} <= ids
        assert "617" not in ids  # HLS VP9 1080p60 duplicates the direct VP9 1080p60 format 303

    def test_recommended_never_empty(self):
        only_hls = [parse_format({"format_id": "1", "ext": "mp4", "protocol": "m3u8_native",
                                  "vcodec": "avc1", "acodec": "none", "height": 720})]
        assert recommended_formats(only_hls) == only_hls

    @pytest.mark.parametrize("codec, family, friendly", [
        ("avc1.64002a", "h264", "H.264"), ("vp09.00.41.08", "vp9", "VP9"), ("vp9", "vp9", "VP9"),
        ("av01.0.13M.08", "av1", "AV1"), ("hev1.1.6.L93", "hevc", "H.265"), ("mp4a.40.2", "aac", "AAC"),
        ("opus", "opus", "Opus"), ("ec-3", "eac3", "E-AC-3"), (None, None, "-"), ("none", None, "-"),
    ])
    def test_codec_names(self, codec, family, friendly):
        assert codec_family(codec) == family and friendly_codec(codec) == friendly

    def test_direct_file_link(self):
        media = parse_info_dict({"id": "x", "title": "clip", "url": "http://h/clip.mp4", "ext": "mp4",
                                 "format_id": "mp4"}, "http://h/clip.mp4")
        assert len(media.formats) == 1 and media.formats[0].kind is FormatKind.COMBINED
        assert media.formats[0].kind_is_guess

    def test_audio_direct_link_guess(self):
        fmt = parse_format({"format_id": "0", "ext": "mp3"})
        assert fmt.kind is FormatKind.AUDIO_ONLY and fmt.kind_is_guess


class TestMediaFromInfo:
    def test_playlist_rejected(self):
        with pytest.raises(InvalidInputError, match="playlist"):
            media_from_info({"_type": "playlist", "entries": [{}, {}]}, "u")

    def test_live_rejected(self):
        with pytest.raises(InvalidInputError, match="live"):
            media_from_info({"id": "x", "is_live": True, "formats": []}, "u")

    def test_drm_only(self):
        with pytest.raises(DownloadError, match="DRM"):
            media_from_info({"id": "x", "formats": [{"format_id": "1", "has_drm": True, "vcodec": "avc1"}]}, "u")

    def test_no_formats(self):
        with pytest.raises(DownloadError):
            media_from_info({"id": "x", "formats": []}, "u")


# ----------------------------------------------------------------------------
# Download plans
# ----------------------------------------------------------------------------
class TestPlans:
    def test_best_available_uses_ytdlp_choice(self, youtube):
        plan = plan_video_download(youtube, None, "auto", True)
        assert plan.format_selector == "401+251" and plan.final_ext == "webm"

    def test_best_available_respects_mp4_preference(self, youtube):
        plan = plan_video_download(youtube, None, "mp4", True)
        assert plan.merge_container == "mp4" and plan.final_ext == "mp4"  # AV1 + Opus can be muxed into MP4

    def test_h264_video_pairs_with_aac_into_mp4(self, youtube):
        plan = plan_video_download(youtube, youtube.get("299"), "auto", True)
        assert plan.format_selector == "299+140" and plan.merge_container == "mp4"
        assert "140-drc" not in plan.format_selector

    def test_vp9_video_pairs_with_opus_into_webm(self, youtube):
        plan = plan_video_download(youtube, youtube.get("303"), "auto", True)
        assert plan.format_selector == "303+251" and plan.final_ext == "webm"

    def test_webm_preference_with_h264_falls_back_to_mkv(self, youtube):
        plan = plan_video_download(youtube, youtube.get("299"), "webm", True)
        assert plan.final_ext == "mkv" and plan.notes

    def test_video_only_without_audio(self, youtube):
        plan = plan_video_download(youtube, youtube.get("299"), "auto", False)
        assert plan.format_selector == "299" and plan.final_ext == "mp4" and plan.notes

    def test_combined_format_downloads_as_is(self, youtube):
        plan = plan_video_download(youtube, youtube.get("18"), "mkv", True)
        assert plan.format_selector == "18" and plan.merge_container is None and plan.final_ext == "mp4"

    def test_unknown_best_falls_back_to_selector(self):
        media = parse_info_dict({"id": "x", "title": "t", "formats": [
            {"format_id": "a", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 720}]}, "u")
        plan = plan_video_download(media, None, "auto", True)
        assert plan.format_selector == "bv*+ba/b" and plan.final_ext is None

    def test_choose_container_rules(self, youtube):
        v_h264, v_vp9, v_av1 = youtube.get("299"), youtube.get("303"), youtube.get("401")
        aac, opus = youtube.get("140"), youtube.get("251")
        assert choose_container(v_h264, aac, "auto")[0] == "mp4"
        assert choose_container(v_vp9, opus, "auto")[0] == "webm"
        assert choose_container(v_h264, opus, "auto")[0] == "mkv"
        assert choose_container(v_av1, aac, "auto")[0] == "mp4"
        assert choose_container(v_vp9, aac, "mkv")[0] == "mkv"

    def test_pick_audio_prefers_original_language(self):
        media = parse_info_dict({"id": "x", "title": "t", "format_id": "v+orig", "formats": [
            {"format_id": "v", "ext": "mp4", "vcodec": "avc1.4d", "acodec": "none", "height": 1080},
            {"format_id": "orig", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 130, "language": "en",
             "language_preference": 10},
            {"format_id": "dub-aac", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 130,
             "language": "de", "language_preference": -1},
            {"format_id": "orig-aac", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128,
             "language": "en", "language_preference": 10},
        ]}, "u")
        assert pick_audio_for_video(media, media.get("v"), "auto").format_id == "orig-aac"

    def test_audio_plan_convert(self, youtube):
        plan = plan_audio_download(youtube, youtube.get("251"), "mp3", 192)
        assert (plan.format_selector, plan.audio_target, plan.audio_bitrate, plan.final_ext) == ("251", "mp3", 192, "mp3")
        assert plan.notes  # 192 kbps from a 129 kbps source

    def test_audio_plan_original(self, youtube):
        assert plan_audio_download(youtube, youtube.get("251"), "original", None).final_ext == "opus"
        assert plan_audio_download(youtube, youtube.get("140"), "original", None).final_ext == "m4a"
        plan = plan_audio_download(youtube, None, "flac", 320)
        assert plan.format_selector == "251" and plan.audio_bitrate is None and plan.final_ext == "flac"

    def test_extracted_audio_ext(self, youtube):
        assert extracted_audio_ext(youtube.get("251")) == "opus"
        assert extracted_audio_ext(youtube.get("139")) == "m4a"


# ----------------------------------------------------------------------------
# yt-dlp messages
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("message, expected", [
    ("ERROR: Unsupported URL: https://example.com", "not supported"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "not a bot"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "403"),
    ("ERROR: Requested format is not available. Use --list-formats", "no longer available"),
    ("ERROR: [generic] Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", "internet"),
    ("ERROR: This video is DRM protected", "DRM"),
    ("something odd happened", "could not be processed"),
])
def test_explain_ytdlp_error(message, expected):
    assert expected.lower() in explain_ytdlp_error(message).lower()


def test_clean_message_strips_colors_and_prefix():
    assert clean_ytdlp_message("\x1b[0;31mERROR:\x1b[0m boom") == "boom"


def test_version_age():
    assert ytdlp_version_age_days("2000.01.01") > 9000
    assert ytdlp_version_age_days("garbage") is None
