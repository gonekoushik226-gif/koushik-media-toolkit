"""Video and audio operations against real FFmpeg (skipped if FFmpeg is missing)."""

import threading

import pytest

from app.core.errors import InvalidInputError, JobCancelled
from app.core.jobs import JobContext
from app.services.audio import AudioService
from app.services.ffmpeg.probe import probe
from app.services.video import VideoService

pytestmark = pytest.mark.ffmpeg


@pytest.fixture
def video(media_tools):
    return VideoService(media_tools)


@pytest.fixture
def audio(media_tools):
    return AudioService(media_tools)


def info_of(media_tools, path):
    return probe(media_tools.ffprobe, path)


def no_leftovers(folder):
    return not [p for p in folder.iterdir() if "mt-partial" in p.name]


class TestVideo:
    def test_info(self, video, sample_video):
        result = video.info(sample_video, JobContext())
        assert "H264 320x240" in result.details and "AAC" in result.details

    @pytest.mark.parametrize("precise", [True, False])
    def test_trim(self, video, media_tools, sample_video, tmp_path, precise):
        out = tmp_path / "clip.mp4"
        progress = []
        ctx = JobContext(on_progress=lambda f, d: progress.append(f), min_interval=0)
        video.trim(sample_video, out, 0.5, 2.0, precise, ctx)
        info = info_of(media_tools, out)
        assert info.has_video and info.has_audio
        expected = 1.5 if precise else None
        if expected:
            assert info.duration == pytest.approx(expected, abs=0.15)
        else:
            assert 1.0 < info.duration <= 2.6
        assert 1.0 in progress and no_leftovers(tmp_path)  # verification afterwards is indeterminate

    def test_trim_invalid_range(self, video, sample_video, tmp_path):
        with pytest.raises(InvalidInputError):
            video.trim(sample_video, tmp_path / "x.mp4", 2.0, 1.0, True, JobContext())
        with pytest.raises(InvalidInputError):
            video.trim(sample_video, tmp_path / "x.mp4", 10.0, None, True, JobContext())

    def test_merge_compatible_uses_fast_join(self, video, media_tools, sample_video, sample_video_2, tmp_path):
        out = tmp_path / "joined.mp4"
        result = video.merge([sample_video, sample_video_2], out, JobContext())
        assert "without re-encoding" in result.message
        assert info_of(media_tools, out).duration == pytest.approx(5.0, abs=0.3)

    def test_merge_different_files_reencodes(self, video, media_tools, sample_video, sample_video_other, tmp_path):
        out = tmp_path / "mixed.mp4"
        video.merge([sample_video, sample_video_other], out, JobContext())
        info = info_of(media_tools, out)
        assert info.duration == pytest.approx(5.0, abs=0.4)
        assert (info.primary_video.width, info.primary_video.height) == (320, 240)
        assert info.has_audio  # the silent clip got generated silence

    def test_extract_audio_copy_and_convert(self, video, media_tools, sample_video, tmp_path):
        copy_out = tmp_path / "sound.m4a"
        video.extract_audio(sample_video, copy_out, "copy", None, JobContext())
        info = info_of(media_tools, copy_out)
        assert info.primary_audio.codec == "aac" and not info.has_video
        mp3_out = tmp_path / "sound.mp3"
        video.extract_audio(sample_video, mp3_out, "mp3", 160, JobContext())
        assert info_of(media_tools, mp3_out).primary_audio.codec == "mp3"

    def test_extract_audio_without_audio(self, video, sample_video_other, tmp_path):
        with pytest.raises(InvalidInputError):
            video.extract_audio(sample_video_other, tmp_path / "a.m4a", "copy", None, JobContext())

    def test_remove_audio_and_extract_video(self, video, media_tools, sample_video, tmp_path):
        muted = tmp_path / "muted.mp4"
        video.remove_audio(sample_video, muted, JobContext())
        assert not info_of(media_tools, muted).has_audio
        only = tmp_path / "video_only.mp4"
        video.extract_video(sample_video, only, JobContext())
        info = info_of(media_tools, only)
        assert info.has_video and not info.has_audio

    def test_convert_remux_and_reencode(self, video, media_tools, sample_video, tmp_path):
        mkv = tmp_path / "out.mkv"
        result = video.convert(sample_video, mkv, False, JobContext())
        assert "no quality loss" in result.message and info_of(media_tools, mkv).format_name.startswith("matroska")
        webm = tmp_path / "out.webm"
        video.convert(sample_video, webm, False, JobContext())
        info = info_of(media_tools, webm)
        assert info.primary_video.codec == "vp9" and info.primary_audio.codec == "opus"

    def test_resize(self, video, media_tools, sample_video, tmp_path):
        out = tmp_path / "small.mp4"
        video.resize(sample_video, out, None, 120, True, JobContext())
        stream = info_of(media_tools, out).primary_video
        assert (stream.width, stream.height) == (160, 120)

    @pytest.mark.parametrize("op, size", [("cw", (240, 320)), ("180", (320, 240)), ("hflip", (320, 240))])
    def test_transform(self, video, media_tools, sample_video, tmp_path, op, size):
        out = tmp_path / f"{op}.mp4"
        video.transform(sample_video, out, op, JobContext())
        stream = info_of(media_tools, out).primary_video
        assert (stream.width, stream.height) == size

    def test_speed(self, video, media_tools, sample_video, tmp_path):
        out = tmp_path / "fast.mp4"
        video.change_speed(sample_video, out, 2.0, JobContext())
        assert info_of(media_tools, out).duration == pytest.approx(1.5, abs=0.25)
        with pytest.raises(InvalidInputError):
            video.change_speed(sample_video, out, 10, JobContext())

    def test_volume(self, video, media_tools, sample_video, tmp_path):
        out = tmp_path / "loud.mp4"
        video.change_volume(sample_video, out, 1.5, JobContext())
        assert info_of(media_tools, out).has_audio

    def test_compress(self, video, media_tools, sample_video, tmp_path):
        out = tmp_path / "compressed.mp4"
        result = video.compress(sample_video, out, "small", 120, JobContext())
        assert info_of(media_tools, out).primary_video.height == 120
        assert result.outputs == [out]

    def test_existing_output_is_replaced_only_when_finished(self, video, sample_video, tmp_path):
        out = tmp_path / "exists.mp4"
        out.write_text("old")
        video.trim(sample_video, out, 0, 1, True, JobContext())
        assert out.stat().st_size > 100 and no_leftovers(tmp_path)

    def test_cancel_leaves_no_files(self, video, sample_video, tmp_path):
        ctx = JobContext()
        out = tmp_path / "cancelled.webm"
        timer = threading.Timer(0.3, ctx.cancel)
        timer.start()
        with pytest.raises(JobCancelled):
            # VP9 encoding of a longer clip is slow enough to be cancelled mid-way.
            video.change_speed(sample_video, out, 0.25, ctx)
        timer.cancel()
        assert not out.exists() and no_leftovers(tmp_path)

    def test_failure_keeps_no_output(self, video, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00" * 2048)
        with pytest.raises(InvalidInputError):
            video.trim(broken, tmp_path / "out.mp4", 0, 1, True, JobContext())
        assert not (tmp_path / "out.mp4").exists()


class TestAudio:
    def test_trim(self, audio, media_tools, sample_audio, tmp_path):
        out = tmp_path / "part.mp3"
        audio.trim(sample_audio, out, 1.0, 3.0, JobContext())
        assert info_of(media_tools, out).duration == pytest.approx(2.0, abs=0.2)

    def test_merge_different_formats(self, audio, media_tools, sample_audio, sample_audio_2, tmp_path):
        out = tmp_path / "joined.m4a"
        audio.merge([sample_audio, sample_audio_2], out, 192, JobContext())
        info = info_of(media_tools, out)
        assert info.duration == pytest.approx(6.0, abs=0.3) and info.primary_audio.codec == "aac"

    def test_merge_same_format_fast(self, audio, media_tools, sample_audio, tmp_path):
        out = tmp_path / "twice.mp3"
        audio.merge([sample_audio, sample_audio], out, 192, JobContext())
        assert info_of(media_tools, out).duration == pytest.approx(8.0, abs=0.3)

    @pytest.mark.parametrize("fmt, codec", [("mp3", "mp3"), ("m4a", "aac"), ("wav", "pcm_s16le"),
                                            ("flac", "flac"), ("opus", "opus"), ("ogg", "vorbis")])
    def test_convert(self, audio, media_tools, sample_audio, tmp_path, fmt, codec):
        out = tmp_path / f"converted.{fmt}"
        audio.convert(sample_audio, out, fmt, 192, JobContext())
        info = info_of(media_tools, out)
        assert info.primary_audio.codec == codec and info.duration == pytest.approx(4.0, abs=0.2)

    def test_convert_warns_about_upsampling_bitrate(self, audio, sample_audio, tmp_path):
        result = audio.convert(sample_audio, tmp_path / "big.mp3", "mp3", 320, JobContext())
        assert result.warnings

    def test_extract_from_video(self, audio, media_tools, sample_video, tmp_path):
        out = tmp_path / "from_video.mp3"
        audio.convert(sample_video, out, "mp3", 128, JobContext())
        info = info_of(media_tools, out)
        assert info.has_audio and not info.has_video

    def test_volume_and_fade(self, audio, media_tools, sample_audio, tmp_path):
        quiet = tmp_path / "quiet.mp3"
        audio.change_volume(sample_audio, quiet, 0.5, JobContext())
        faded = tmp_path / "faded.mp3"
        audio.fade(sample_audio, faded, 1.0, 1.0, JobContext())
        assert info_of(media_tools, faded).duration == pytest.approx(4.0, abs=0.2)

    def test_tags_round_trip_in_place(self, audio, sample_audio, tmp_path):
        import shutil

        song = tmp_path / "song.mp3"
        shutil.copy(sample_audio, song)
        assert audio.read_tags(song)["title"] == "Test Tone"
        audio.write_tags(song, song, {"title": "New Title", "artist": "Someone", "album": "Ä Album"}, JobContext())
        tags = audio.read_tags(song)
        assert tags["title"] == "New Title" and tags["artist"] == "Someone" and tags["album"] == "Ä Album"
        assert no_leftovers(tmp_path)

    def test_tags_in_opus(self, audio, sample_audio, tmp_path):
        opus = tmp_path / "t.opus"
        audio.convert(sample_audio, opus, "opus", 96, JobContext())
        tagged = tmp_path / "tagged.opus"
        audio.write_tags(opus, tagged, {"title": "Opus Title"}, JobContext())
        assert audio.read_tags(tagged)["title"] == "Opus Title"
