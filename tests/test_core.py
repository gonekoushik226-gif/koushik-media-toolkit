import errno
import json
import threading
import time

import pytest

from app.config.settings import Settings, SettingsStore, settings_from_dict
from app.core.errors import AppError, DownloadError, JobCancelled, describe_exception
from app.core.jobs import JobContext, run_abandonable
from app.core.registry import ModuleRegistry, ModuleSpec


class TestJobContext:
    def test_progress_and_status_callbacks(self):
        seen = []
        ctx = JobContext(on_status=lambda s: seen.append(("status", s)),
                         on_progress=lambda f, d: seen.append(("progress", f, d)), min_interval=0)
        ctx.set_status("Working")
        ctx.set_progress(0.5, "half")
        ctx.step(3, 4)
        assert seen == [("status", "Working"), ("progress", 0.5, "half"), ("progress", 0.75, "3 / 4")]

    def test_progress_is_clamped_and_throttled(self):
        seen = []
        ctx = JobContext(on_progress=lambda f, d: seen.append(f), min_interval=10)
        ctx.set_progress(0.1)
        ctx.set_progress(0.2)  # throttled
        ctx.set_progress(5)  # edge value (clamped to 1.0) always passes
        assert seen == [0.1, 1.0]

    def test_cancel_runs_callbacks_once(self):
        ctx = JobContext()
        calls = []
        ctx.add_cancel_callback(lambda: calls.append(1))
        ctx.cancel()
        ctx.cancel()
        assert calls == [1] and ctx.is_cancelled
        with pytest.raises(JobCancelled):
            ctx.check_cancelled()

    def test_callback_added_after_cancel_runs_immediately(self):
        ctx = JobContext()
        ctx.cancel()
        calls = []
        ctx.add_cancel_callback(lambda: calls.append(1))
        assert calls == [1]

    def test_remove_callback(self):
        ctx = JobContext()
        calls = []
        remove = ctx.add_cancel_callback(lambda: calls.append(1))
        remove()
        ctx.cancel()
        assert calls == []

    def test_resolve_outputs_defaults_to_keep_both(self, tmp_path):
        existing = tmp_path / "a.pdf"
        existing.write_text("x")
        result = JobContext().resolve_outputs([existing, tmp_path / "b.pdf"])
        assert [p.name for p in result] == ["a (1).pdf", "b.pdf"]

    def test_resolve_outputs_uses_resolver(self, tmp_path):
        existing = tmp_path / "a.pdf"
        existing.write_text("x")
        ctx = JobContext(resolver=lambda planned, found: None)
        with pytest.raises(JobCancelled):
            ctx.resolve_outputs([existing])
        ctx = JobContext(resolver=lambda planned, found: planned)
        assert ctx.resolve_outputs([existing]) == [existing]

    def test_run_abandonable_returns_result_and_errors(self):
        assert run_abandonable(lambda: 42, JobContext()) == 42
        with pytest.raises(ValueError):
            run_abandonable(lambda: (_ for _ in ()).throw(ValueError("x")), JobContext())

    def test_run_abandonable_stops_waiting_on_cancel(self):
        ctx = JobContext()
        release = threading.Event()
        threading.Timer(0.2, ctx.cancel).start()
        started = time.monotonic()
        with pytest.raises(JobCancelled):
            run_abandonable(lambda: release.wait(10), ctx)
        assert time.monotonic() - started < 3
        release.set()


class TestErrors:
    def test_app_error_passthrough(self):
        report = describe_exception(DownloadError("Nope", details="HTTP 404"))
        assert (report.title, report.message, report.details) == ("Download failed", "Nope", "HTTP 404")

    def test_permission_error(self):
        report = describe_exception(PermissionError(13, "Access is denied", "C:\\x.mp4"))
        assert report.title == "Permission denied" and "C:\\x.mp4" in report.message

    def test_disk_full(self):
        report = describe_exception(OSError(errno.ENOSPC, "No space left on device"))
        assert report.title == "Disk full"

    def test_unexpected_error_hides_traceback(self):
        report = describe_exception(KeyError("boom"))
        assert report.unexpected and "Traceback" not in report.message and "KeyError" in report.details

    def test_app_error_title_override(self):
        assert AppError("x", title="Custom").title == "Custom"


class TestSettings:
    def test_round_trip(self, tmp_path):
        store = SettingsStore(tmp_path / "settings.json")
        settings = Settings(theme="dark", audio_bitrate=320, pdf_dpi=300, output_dir="D:\\out")
        store.save(settings)
        assert store.load() == settings

    def test_missing_file_gives_defaults(self, tmp_path):
        assert SettingsStore(tmp_path / "none.json").load() == Settings()

    def test_invalid_values_fall_back(self):
        loaded = settings_from_dict({"theme": "purple", "audio_bitrate": 999, "pdf_dpi": 5000,
                                     "confirm": True, "open_folder_after": "yes", "video_container": "mkv"})
        assert loaded.theme == "system" and loaded.audio_bitrate == 192 and loaded.pdf_dpi == 150
        assert loaded.open_folder_after is False and loaded.video_container == "mkv"

    def test_corrupt_file_is_backed_up(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{not json", encoding="utf-8")
        assert SettingsStore(path).load() == Settings()
        assert (tmp_path / "settings.corrupt.json").exists()

    def test_saved_file_is_valid_json(self, tmp_path):
        path = tmp_path / "s.json"
        SettingsStore(path).save(Settings())
        assert json.loads(path.read_text(encoding="utf-8"))["overwrite_policy"] == "ask"

    def test_output_dir_for(self, tmp_path):
        assert Settings().output_dir_for(tmp_path / "in.mp4") == tmp_path
        assert Settings(output_dir="C:\\Out").output_dir_for(tmp_path / "in.mp4").name == "Out"


class TestRegistry:
    def test_register_and_order(self):
        registry = ModuleRegistry()
        registry.register(ModuleSpec("b", "B", "", "x", lambda ctx: None, order=2))
        registry.register(ModuleSpec("a", "A", "", "x", lambda ctx: None, order=1))
        registry.register(ModuleSpec("u", "U", "", "x", lambda ctx: None, group="utility"))
        assert [s.key for s in registry.specs("main")] == ["a", "b"]
        assert "u" in registry and registry.get("u").title == "U"

    def test_duplicate_key_rejected(self):
        registry = ModuleRegistry()
        registry.register(ModuleSpec("a", "A", "", "x", lambda ctx: None))
        with pytest.raises(ValueError):
            registry.register(ModuleSpec("a", "A2", "", "x", lambda ctx: None))
