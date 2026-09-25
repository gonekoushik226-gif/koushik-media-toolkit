"""Output formats and the encoder settings used for them."""

from __future__ import annotations

from dataclasses import dataclass

# ----------------------------------------------------------------------------
# Video containers
# ----------------------------------------------------------------------------
VIDEO_OUTPUT_FORMATS: dict[str, str] = {
    "mp4": "MP4 (H.264 + AAC) - plays everywhere",
    "mkv": "MKV (H.264 + AAC)",
    "mov": "MOV (H.264 + AAC)",
    "webm": "WEBM (VP9 + Opus)",
    "avi": "AVI (MPEG-4 + MP3) - for old devices",
}
VIDEO_INPUT_EXTENSIONS = (
    "mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv", "flv", "mpg", "mpeg", "ts", "m2ts", "mts", "3gp", "ogv",
)


def video_output_ext(input_ext: str) -> str:
    """Keep the input's container when we know how to write it, else MP4."""
    ext = input_ext.lower().lstrip(".")
    return ext if ext in VIDEO_OUTPUT_FORMATS else "mp4"


def video_encoder_args(ext: str, crf: int = 20, preset: str = "medium") -> list[str]:
    ext = ext.lower().lstrip(".")
    if ext == "webm":
        # VP9's quality scale differs from x264's; +12 gives comparable results.
        return ["-c:v", "libvpx-vp9", "-crf", str(min(63, crf + 12)), "-b:v", "0",
                "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p"]
    if ext == "avi":
        return ["-c:v", "mpeg4", "-q:v", "3", "-vtag", "xvid"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]


def video_audio_encoder_args(ext: str, bitrate_kbps: int = 192) -> list[str]:
    ext = ext.lower().lstrip(".")
    if ext == "webm":
        return ["-c:a", "libopus", "-b:a", f"{bitrate_kbps}k"]
    if ext == "avi":
        return ["-c:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k"]
    return ["-c:a", "aac", "-b:a", f"{bitrate_kbps}k"]


def container_extra_args(ext: str) -> list[str]:
    """Muxer options: MP4/MOV get the index at the front for quick playback start."""
    if ext.lower().lstrip(".") in ("mp4", "mov", "m4v", "m4a"):
        return ["-movflags", "+faststart"]
    return []


# ----------------------------------------------------------------------------
# Audio formats
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class AudioFormat:
    key: str
    ext: str
    label: str
    codec: str
    lossless: bool
    uses_bitrate: bool
    keeps_cover_art: bool


AUDIO_FORMATS: dict[str, AudioFormat] = {
    "mp3": AudioFormat("mp3", "mp3", "MP3", "libmp3lame", False, True, True),
    "m4a": AudioFormat("m4a", "m4a", "M4A (AAC)", "aac", False, True, True),
    "wav": AudioFormat("wav", "wav", "WAV (uncompressed)", "pcm_s16le", True, False, False),
    "flac": AudioFormat("flac", "flac", "FLAC (lossless)", "flac", True, False, True),
    "opus": AudioFormat("opus", "opus", "OPUS", "libopus", False, True, False),
    "ogg": AudioFormat("ogg", "ogg", "OGG (Vorbis)", "libvorbis", False, True, False),
}
_EXT_ALIASES = {"mp3": "mp3", "m4a": "m4a", "aac": "m4a", "wav": "wav", "flac": "flac", "opus": "opus", "ogg": "ogg", "oga": "ogg"}
AUDIO_INPUT_EXTENSIONS = (
    "mp3", "m4a", "aac", "wav", "flac", "opus", "ogg", "oga", "wma", "aiff", "aif", "alac", "ac3", "mka", "webm", "amr",
)
STANDARD_BITRATES = (64, 96, 128, 160, 192, 256, 320)


def audio_format_for_ext(ext: str) -> AudioFormat | None:
    key = _EXT_ALIASES.get(ext.lower().lstrip("."))
    return AUDIO_FORMATS.get(key) if key else None


def audio_output_ext(input_ext: str) -> str:
    """Keep the input's format when we can encode it, otherwise use M4A."""
    fmt = audio_format_for_ext(input_ext)
    return fmt.ext if fmt else "m4a"


def audio_encoder_args(fmt: AudioFormat, bitrate_kbps: int | None = None, high_bit_depth: bool = False) -> list[str]:
    codec = fmt.codec
    if fmt.key == "wav" and high_bit_depth:
        codec = "pcm_s24le"
    args = ["-c:a", codec]
    if fmt.uses_bitrate:
        args += ["-b:a", f"{bitrate_kbps or 192}k"]
    if fmt.key == "mp3":
        args += ["-id3v2_version", "3"]  # tags Windows Explorer can read
    return args


def nearest_bitrate(bits_per_second: int | None, default: int = 192) -> int:
    """Pick a standard bitrate close to the source's (used when re-encoding
    'in the same format' so quality is not silently reduced)."""
    if not bits_per_second:
        return default
    kbps = bits_per_second / 1000
    return min(STANDARD_BITRATES, key=lambda b: abs(b - kbps))


def copy_audio_ext(codec: str) -> str:
    """File extension that can hold ``codec`` without re-encoding."""
    codec = (codec or "").lower()
    if codec.startswith("pcm_"):
        return "wav"
    return {
        "aac": "m4a",
        "mp3": "mp3",
        "opus": "opus",
        "vorbis": "ogg",
        "flac": "flac",
        "alac": "m4a",
        "ac3": "ac3",
        "eac3": "eac3",
        "wmav1": "wma",
        "wmav2": "wma",
    }.get(codec, "mka")


_LOSSLESS_AUDIO = ("flac", "alac", "wavpack", "ape", "tta", "truehd", "mlp")


def is_high_bit_depth(sample_fmt: str, bits: int | None, codec: str = "") -> bool:
    """True for genuinely high-resolution sources (24-bit FLAC/WAV...). Lossy
    decoders (MP3, AAC, Opus) always output float samples, which does not
    mean the source has more than 16 bits of real resolution."""
    if (bits or 0) > 16:
        return True
    lossless = codec.startswith("pcm_") or codec in _LOSSLESS_AUDIO
    return lossless and sample_fmt.startswith(("s32", "flt", "dbl"))
