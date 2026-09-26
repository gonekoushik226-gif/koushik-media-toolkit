"""Headless GUI tests: every page opens, and real jobs run through the panels."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.gui


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    from app.ui.theme import apply_theme

    app = QApplication.instance() or QApplication([])
    apply_theme(app, "light")
    return app


@pytest.fixture
def dialogs(monkeypatch):
    """Replace modal dialogs with recorders (a real dialog would block the test)."""
    seen: dict[str, list] = {"error": [], "warning": [], "info": []}
    import app.ui.dialogs as dialogs_module
    import app.ui.pages.base as base_module

    def record(kind):
        return lambda *args, **kwargs: seen[kind].append(args)

    for module in (dialogs_module, base_module):
        monkeypatch.setattr(module, "show_error", record("error"), raising=False)
    monkeypatch.setattr(dialogs_module, "show_warning", record("warning"))
    monkeypatch.setattr(dialogs_module, "show_info", record("info"))
    return seen


@pytest.fixture
def window(qapp, tmp_path):
    from app.config.credentials import MemorySecretStore
    from app.config.settings import Settings, SettingsStore
    from app.services.tools import ToolLocator
    from app.services.translation.keys import ApiKeyManager
    from app.ui.context import AppContext
    from app.ui.main_window import MainWindow
    from app.ui.modules import build_registry

    # API keys live in memory only: tests never touch the real Windows Credential Manager entry.
    ctx = AppContext(settings=Settings(output_dir=str(tmp_path / "out")), store=SettingsStore(tmp_path / "s.json"),
                     tools=None, api_keys=ApiKeyManager(MemorySecretStore()))
    ctx.tools = ToolLocator(lambda: ctx.settings)
    win = MainWindow(ctx, build_registry())
    win.show()
    yield win
    win.ctx.jobs.shutdown()
    win.close()


def wait_until(qapp, condition, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            for _ in range(5):
                qapp.processEvents()
            return True
        time.sleep(0.02)
    return False


def wait_for_job(qapp, window, timeout=90.0):
    assert wait_until(qapp, lambda: not window.ctx.jobs.busy, timeout), "job did not finish in time"


def test_every_module_opens(window, qapp):
    for spec in window.registry.specs():
        window.navigate(spec.key)
        qapp.processEvents()
        assert window.current_page() is window.page(spec.key)
        pages = getattr(window.page(spec.key), "_operations", None)
        if pages:  # open every operation panel too
            for key, _, _ in pages:
                window.page(spec.key).show_operation(key)
                qapp.processEvents()
                assert window.page(spec.key).panel(key) is not None
    window.navigate("home")
    assert window.current_page() is window.home and not window.header.isVisible()


def test_download_hub_navigates(window, qapp):
    window.navigate("download")
    window.ctx.navigate("pdf", "download")
    assert window.page("pdf").stack.currentWidget().widget().key == "download"


def test_merge_pdfs_through_panel(window, qapp, dialogs, pdf_factory, tmp_path):
    window.navigate("pdf", "merge")
    panel = window.page("pdf").panel("merge")
    panel.files.add_paths([pdf_factory("a.pdf", 2), pdf_factory("b.pdf", 3)])
    panel.output.folder.setText(str(tmp_path / "result"))
    panel.output.name.setText("joined")
    panel.action_button.click()
    wait_for_job(qapp, window)
    output = tmp_path / "result" / "joined.pdf"
    assert output.exists() and not dialogs["error"]
    from app.services.pdf import count_pages

    assert count_pages(output) == 5
    assert "Merged 2 PDFs" in window.status._detail.text()


def test_existing_output_asks_and_can_cancel(window, qapp, dialogs, pdf_factory, tmp_path, monkeypatch):
    import app.ui.dialogs as dialogs_module

    asked = []
    monkeypatch.setattr(dialogs_module, "resolve_conflicts", lambda parent, planned, existing, policy: asked.append(1))
    window.navigate("pdf", "merge")
    panel = window.page("pdf").panel("merge")
    panel.files.add_paths([pdf_factory("a.pdf"), pdf_factory("b.pdf")])
    panel.output.folder.setText(str(tmp_path))
    panel.output.name.setText("a")  # a.pdf already exists
    panel.action_button.click()
    qapp.processEvents()
    assert asked and not window.ctx.jobs.busy  # user cancelled -> nothing started


def test_validation_error_is_shown(window, qapp, dialogs):
    window.navigate("pdf", "merge")
    window.page("pdf").panel("merge").action_button.click()
    assert dialogs["error"] and "at least two" in dialogs["error"][0][1].message


def test_split_preview_and_run(window, qapp, dialogs, pdf_factory, tmp_path):
    window.navigate("pdf", "split")
    panel = window.page("pdf").panel("split")
    panel.picker.set_path(pdf_factory("doc.pdf", 10))
    assert wait_until(qapp, lambda: panel.pages == 10, 20)
    panel.parts.setValue(3)
    assert "4, 3, 3" in panel.preview.text()
    panel.output.folder.setText(str(tmp_path / "parts"))
    panel.action_button.click()
    wait_for_job(qapp, window)
    assert sorted(p.name for p in (tmp_path / "parts").iterdir()) == ["doc_part01.pdf", "doc_part02.pdf", "doc_part03.pdf"]


def test_images_page_edit_and_save(window, qapp, dialogs, image_factory, tmp_path):
    from PIL import Image

    window.navigate("images")
    page = window.page("images")
    page.files.add_paths([image_factory("a.png", size=(64, 48)), image_factory("b.png", size=(64, 48))])
    page.files.list.setCurrentRow(0)
    assert wait_until(qapp, lambda: page._current in page._previews, 20)
    page.scope_all.setChecked(True)
    page._add_op(__import__("app.services.images", fromlist=["rotate_op"]).rotate_op(90))
    page.save_format.setCurrentIndex(page.save_format.findData("jpg"))
    page.save_folder.setText(str(tmp_path / "saved"))
    page.save_button.click()
    wait_for_job(qapp, window)
    saved = sorted((tmp_path / "saved").iterdir())
    assert [p.name for p in saved] == ["a_edited.jpg", "b_edited.jpg"]
    with Image.open(saved[0]) as img:
        assert img.size == (48, 64)


@pytest.mark.ffmpeg
def test_trim_panel_with_ffmpeg(window, qapp, dialogs, sample_video, tmp_path):
    window.navigate("video", "trim")
    panel = window.page("video").panel("trim")
    panel.picker.set_path(sample_video)
    assert wait_until(qapp, lambda: panel.media_info is not None, 30)
    assert panel.end.text().startswith("00:00:03")
    panel.start.setText("00:00:01")
    panel.output.folder.setText(str(tmp_path))
    panel.action_button.click()
    wait_for_job(qapp, window)
    output = tmp_path / "sample_trimmed.mp4"
    assert output.exists() and not dialogs["error"], dialogs["error"]


@pytest.mark.ffmpeg
def test_cancel_from_status_area(window, qapp, dialogs, sample_video, tmp_path):
    window.navigate("video", "speed")
    panel = window.page("video").panel("speed")
    panel.picker.set_path(sample_video)
    panel.speed.setValue(0.25)
    panel.output.folder.setText(str(tmp_path))
    panel.output.set_extension("webm")  # slow VP9 encode, so there is time to cancel
    panel.action_button.click()
    assert wait_until(qapp, lambda: window.ctx.jobs.busy, 10)
    window.status.cancel_requested.emit()
    wait_for_job(qapp, window)
    assert window.status._title.text() == "Cancelled"
    assert not list(tmp_path.glob("*.webm")) and not list(tmp_path.glob("*mt-partial*"))


def test_invalid_url_is_rejected(window, qapp, dialogs):
    window.navigate("video", "download")
    panel = window.page("video").panel("download")
    panel.url.setText("file:///C:/Windows/win.ini")
    panel.analyze()
    assert dialogs["error"] and not window.ctx.jobs.busy


# ----------------------------------------------------------------------------
# Image folder -> PDF naming
# ----------------------------------------------------------------------------
def _image_folder(tmp_path, name, count=3):
    from PIL import Image

    folder = tmp_path / name
    folder.mkdir()
    for number in range(1, count + 1):
        Image.new("RGB", (40, 30), (number * 20 % 256, 90, 160)).save(folder / f"{number}.png")
    return folder


def test_create_pdf_from_folder_uses_folder_name(window, qapp, dialogs, tmp_path):
    from app.services.pdf import count_pages

    folder = _image_folder(tmp_path, "My Comic Chapter 01", 12)
    window.navigate("pdf", "create")
    panel = window.page("pdf").panel("create")
    assert panel.files.add_folder(folder) == 12
    assert [p.name for p in panel.files.paths()][:3] == ["1.png", "2.png", "3.png"]
    assert panel.files.paths()[-1].name == "12.png"  # natural order: 1, 2, ... 10, 11, 12
    assert panel.output.name.text() == "My Comic Chapter 01"
    panel.output.folder.setText(str(tmp_path / "pdfs"))
    panel.action_button.click()
    wait_for_job(qapp, window)
    output = tmp_path / "pdfs" / "My Comic Chapter 01.pdf"
    assert output.exists() and count_pages(output) == 12 and not dialogs["error"]


def test_folder_pdf_never_silently_replaces(window, qapp, dialogs, tmp_path):
    folder = _image_folder(tmp_path, "漫画 第1話", 2)
    existing = tmp_path / "pdfs" / "漫画 第1話.pdf"
    existing.parent.mkdir()
    existing.write_bytes(b"%PDF-1.4 keep me")
    window.ctx.settings.overwrite_policy = "rename"
    window.navigate("pdf", "create")
    panel = window.page("pdf").panel("create")
    panel.files.add_folder(folder)
    assert panel.output.name.text() == "漫画 第1話"
    panel.output.folder.setText(str(existing.parent))
    panel.action_button.click()
    wait_for_job(qapp, window)
    assert existing.read_bytes() == b"%PDF-1.4 keep me"  # the old PDF is untouched
    assert len(list(existing.parent.glob("漫画 第1話*.pdf"))) == 2


def test_images_page_folder_names_pdf(window, qapp, dialogs, tmp_path):
    folder = _image_folder(tmp_path, "Chapter 7 - The End", 2)
    window.navigate("images")
    page = window.page("images")
    page.files.add_folder(folder)
    assert page.pdf_output.name.text() == "Chapter 7 - The End"


# ----------------------------------------------------------------------------
# AI translation (fake provider - no network, no real key)
# ----------------------------------------------------------------------------
FAKE_KEY = "AIza" + "GuiTest" + "k" * 28  # built at run time; not a real key


@pytest.fixture
def translate_dialogs(monkeypatch):
    import app.ui.pages.translate as translate_module

    seen = {"setup": [], "info": [], "error": [], "confirm": []}
    monkeypatch.setattr(translate_module, "ask_to_set_up_key", lambda *a: seen["setup"].append(a))
    monkeypatch.setattr(translate_module, "show_info", lambda *a: seen["info"].append(a))
    monkeypatch.setattr(translate_module, "show_error", lambda *a: seen["error"].append(a))
    monkeypatch.setattr(translate_module, "confirm", lambda *a: seen["confirm"].append(a) or True)
    return seen


def test_translate_without_key_shows_setup(window, qapp, dialogs, translate_dialogs, tmp_path):
    from tests.translation_helpers import make_text_pdf

    source = make_text_pdf(tmp_path / "Book.pdf", pages=1)
    window.navigate("translate")
    panel = window.page("translate").panel("translate")
    assert "No API key" in panel.key_status.text()
    panel.picker.set_path(source)
    panel.action_button.click()
    assert translate_dialogs["setup"] and not window.ctx.jobs.busy
    assert not list(tmp_path.glob("Book - *.pdf"))


def test_api_key_panel_save_replace_session_remove(window, qapp, dialogs, translate_dialogs):
    manager = window.ctx.key_manager()
    window.navigate("translate", "key")
    panel = window.page("translate").panel("key")
    assert panel.key_edit.echoMode() == panel.key_edit.EchoMode.Password
    panel.key_edit.setText("not a key")
    panel.action_button.click()  # Save
    assert dialogs["error"] and manager.key() is None
    panel.key_edit.setText(FAKE_KEY)
    panel.action_button.click()
    assert manager.key() == FAKE_KEY and panel.key_edit.text() == ""
    assert FAKE_KEY not in panel.status.text() and "kkkk" in panel.status.text()
    replacement = FAKE_KEY[:-4] + "zzzz"
    panel.key_edit.setText(replacement)
    panel.action_button.click()
    assert manager.key() == replacement and "replaced" in translate_dialogs["info"][-1][2]
    panel.key_edit.setText(FAKE_KEY)
    panel.session_button.click()
    assert manager.key() == FAKE_KEY and manager.store.get(manager.name) == replacement
    panel.remove_button.click()
    assert manager.key() is None and not manager.is_saved()
    assert "No API key" in panel.status.text()


def test_api_key_test_button(window, qapp, dialogs, translate_dialogs):
    from app.services.translation.provider import ApiKeyError
    from tests.translation_helpers import FakeProvider

    class BadKeyProvider(FakeProvider):
        def list_models(self, ctx=None):
            raise ApiKeyError("Google did not accept the API key.")

    window.ctx.provider_factory = lambda key, model: FakeProvider() if key == FAKE_KEY else BadKeyProvider()
    window.navigate("translate", "key")
    panel = window.page("translate").panel("key")
    panel.test_key()
    assert "Paste your API key first" in panel.test_result.text()
    panel.key_edit.setText(FAKE_KEY)
    panel.test_key()
    assert wait_until(qapp, lambda: panel.test_result.text().startswith("✓"), 10), panel.test_result.text()
    assert panel.model.findData("fake-model") >= 0
    panel.key_edit.setText(FAKE_KEY[:-4] + "bbbb")
    panel.test_key()
    assert wait_until(qapp, lambda: panel.test_result.text().startswith("✗"), 10)
    assert "did not accept" in panel.test_result.text() and window.ctx.key_manager().key() is None


def test_translate_pdf_through_panel(window, qapp, dialogs, translate_dialogs, tmp_path):
    import pymupdf

    from tests.translation_helpers import FakeProvider, make_text_pdf, sha256

    source = make_text_pdf(tmp_path / "Japanese Book.pdf", pages=2)
    before = sha256(source)
    window.ctx.key_manager().save(FAKE_KEY)
    window.ctx.provider_factory = lambda key, model: FakeProvider()
    window.navigate("translate")
    panel = window.page("translate").panel("translate")
    panel.refresh_status()
    assert "✓" in panel.key_status.text() and FAKE_KEY not in panel.key_status.text()
    panel.picker.set_path(source)
    assert wait_until(qapp, lambda: panel.plan is not None, 20), panel.input_info.text()
    assert "2 with selectable text" in panel.input_info.text()
    panel.target_language.setCurrentIndex(panel.target_language.findData("fr"))
    assert panel.output.name.text() == "Japanese Book - French"
    panel.output.folder.setText(str(tmp_path / "out"))
    panel.action_button.click()
    wait_for_job(qapp, window)
    output = tmp_path / "out" / "Japanese Book - French.pdf"
    assert output.exists() and not dialogs["error"], dialogs["error"]
    assert sha256(source) == before
    assert window.ctx.settings.translation_target == "fr"
    with pymupdf.open(str(output)) as doc:
        assert doc.page_count == 2 and "Traduit" in doc[1].get_text()


def test_translate_rejects_same_output_and_bad_range(window, qapp, dialogs, translate_dialogs, tmp_path):
    from tests.translation_helpers import FakeProvider, make_text_pdf

    source = make_text_pdf(tmp_path / "Book.pdf", pages=2)
    window.ctx.key_manager().use_for_session(FAKE_KEY)
    window.ctx.provider_factory = lambda key, model: FakeProvider()
    window.navigate("translate")
    panel = window.page("translate").panel("translate")
    panel.picker.set_path(source)
    assert wait_until(qapp, lambda: panel.plan is not None, 20)
    panel.page_range.setText("1-9")
    panel.action_button.click()
    assert dialogs["error"] and not window.ctx.jobs.busy
    panel.page_range.setText("")
    panel.output.folder.setText(str(tmp_path))
    panel.output.name.setText("Book")
    panel.action_button.click()
    assert len(dialogs["error"]) == 2 and not window.ctx.jobs.busy
    assert "cannot replace the original" in dialogs["error"][-1][1].message


def test_translate_image_folder_through_panel(window, qapp, dialogs, translate_dialogs, tmp_path):
    from tests.translation_helpers import FakeProvider

    folder = _image_folder(tmp_path, "Manga Chapter 3", 2)
    window.ctx.key_manager().use_for_session(FAKE_KEY)
    regions = [{"box": (0.1, 0.1, 0.5, 0.3), "kind": "dialogue", "original": "x", "translation": "Hello"}]
    window.ctx.provider_factory = lambda key, model: FakeProvider(regions=regions)
    window.navigate("translate")
    panel = window.page("translate").panel("translate")
    panel.picker.set_path(folder)
    assert panel.doc_type.currentData() == "comic"
    assert wait_until(qapp, lambda: panel.plan is not None, 20)
    assert panel.output.name.text() == "Manga Chapter 3 - English"
    panel.output.folder.setText(str(tmp_path / "out"))
    panel.action_button.click()
    wait_for_job(qapp, window)
    assert (tmp_path / "out" / "Manga Chapter 3 - English.pdf").exists() and not dialogs["error"]


# ----------------------------------------------------------------------------
# Burn in subtitles
# ----------------------------------------------------------------------------
SRT_TEXT = "1\n00:00:00,500 --> 00:00:02,000\nHello from the subtitle file\n"


@pytest.mark.ffmpeg
def test_burn_subtitles_through_panel(window, qapp, dialogs, sample_video, tmp_path):
    import shutil

    from app.services.ffmpeg.probe import probe

    movie = tmp_path / "My Movie.mp4"
    shutil.copy(sample_video, movie)
    (tmp_path / "My Movie.srt").write_text(SRT_TEXT, encoding="utf-8")
    window.navigate("video", "subtitles")
    panel = window.page("video").panel("subtitles")
    panel.picker.set_path(movie)
    assert panel.subtitle.path() == tmp_path / "My Movie.srt"  # found automatically
    assert panel.output.name.text() == "My Movie_subtitled"
    panel.output.folder.setText(str(tmp_path / "out"))
    panel.action_button.click()
    wait_for_job(qapp, window)
    output = tmp_path / "out" / "My Movie_subtitled.mp4"
    assert output.exists() and not dialogs["error"], dialogs["error"]
    info = probe(window.ctx.tools.media_tools().ffprobe, output)
    assert info.has_video and info.has_audio
    assert "1 subtitle(s) burned in" in window.status._detail.text()


def test_burn_subtitles_needs_a_subtitle_file(window, qapp, dialogs, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not really a video")
    window.navigate("video", "subtitles")
    panel = window.page("video").panel("subtitles")
    panel.picker.set_path(video)
    assert panel.subtitle.path() is None
    panel.action_button.click()
    assert dialogs["error"] and not window.ctx.jobs.busy


def test_burn_subtitles_style_controls(window, qapp, dialogs, tmp_path):
    window.navigate("video", "subtitles")
    panel = window.page("video").panel("subtitles")
    ass = tmp_path / "styled.ass"
    ass.write_text("[Script Info]\n", encoding="utf-8")
    srt = tmp_path / "plain.srt"
    srt.write_text(SRT_TEXT, encoding="utf-8")
    panel.subtitle.set_path(srt)
    assert panel.size.isEnabled() and panel.keep_style.isHidden()
    panel.subtitle.set_path(ass)
    assert not panel.keep_style.isHidden() and not panel.size.isEnabled()  # the file's own style is kept
    panel.keep_style.setChecked(False)
    assert panel.size.isEnabled() and not panel.style().keep_file_style
    panel.position.setCurrentIndex(panel.position.findData("top"))
    assert "Alignment=6" in panel.style().force_style()


def test_user_chosen_subtitle_is_not_replaced(window, qapp, dialogs, tmp_path):
    window.navigate("video", "subtitles")
    panel = window.page("video").panel("subtitles")
    chosen = tmp_path / "chosen.srt"
    chosen.write_text(SRT_TEXT, encoding="utf-8")
    panel.subtitle.set_path(chosen)
    video = tmp_path / "Film.mp4"
    video.write_bytes(b"x")
    (tmp_path / "Film.srt").write_text(SRT_TEXT, encoding="utf-8")
    panel.picker.set_path(video)
    assert panel.subtitle.path() == chosen


# ----------------------------------------------------------------------------
# Update notification (GitHub is never contacted: the check is replaced)
# ----------------------------------------------------------------------------
@pytest.fixture
def fake_update(monkeypatch):
    from app.services import updates

    state = {"result": updates.UpdateInfo("9.9.9", "Media Toolkit 9.9.9",
                                          "https://github.com/example/app/releases/tag/v9.9.9", "2030-01-01"),
             "calls": 0, "opened": []}

    def check(*args, **kwargs):
        state["calls"] += 1
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(updates, "check_for_update", check)
    from app.ui.widgets import update_banner

    monkeypatch.setattr(update_banner.QDesktopServices, "openUrl", lambda url: state["opened"].append(url.toString())
                        or True)
    return state


def test_update_banner_download_and_skip(window, qapp, dialogs, fake_update):
    banner = window.update_banner
    assert banner.isHidden() and fake_update["calls"] == 0  # creating the window never checks by itself
    window.check_for_updates()
    assert wait_until(qapp, lambda: not banner.isHidden(), 10)
    assert "9.9.9" in banner.text.text()
    banner.download.click()
    assert fake_update["opened"] == ["https://github.com/example/app/releases/tag/v9.9.9"]
    banner.skip.click()
    assert banner.isHidden() and window.ctx.settings.update_skipped_version == "9.9.9"
    window.check_for_updates()  # automatic check: a skipped version stays quiet
    assert wait_until(qapp, lambda: fake_update["calls"] == 2, 10)
    for _ in range(20):
        qapp.processEvents()
    assert banner.isHidden()
    window.check_for_updates(manual=True)  # asked for explicitly: shown again
    assert wait_until(qapp, lambda: not banner.isHidden(), 10)
    banner.later.click()
    assert banner.isHidden()


def test_banner_only_opens_github(window, qapp, fake_update):
    from app.services.updates import UpdateInfo

    window.update_banner.show_update(UpdateInfo("9.9.9", "x", "https://evil.example.com/setup.exe", ""))
    assert not window.update_banner.open_release_page() and not fake_update["opened"]


def test_manual_check_reports_result(window, qapp, dialogs, fake_update, monkeypatch):
    import app.ui.main_window as main_window_module
    from app.services.updates import UpdateCheckError

    fake_update["result"] = None
    window.check_for_updates(manual=True)
    assert wait_until(qapp, lambda: bool(dialogs["info"]), 10)
    assert "latest version" in dialogs["info"][-1][2]
    errors = []
    monkeypatch.setattr(main_window_module, "show_error", lambda *a: errors.append(a))
    fake_update["result"] = UpdateCheckError("Could not reach GitHub.")
    window.check_for_updates(manual=False)  # automatic: failures stay quiet
    assert wait_until(qapp, lambda: fake_update["calls"] == 2, 10)
    for _ in range(20):
        qapp.processEvents()
    assert not errors
    window.check_for_updates(manual=True)
    assert wait_until(qapp, lambda: bool(errors), 10)


def test_update_setting_and_check_button(window, qapp, dialogs, fake_update, monkeypatch):
    import app.ui.pages.settings as settings_module

    monkeypatch.setattr(settings_module, "show_info", lambda *a: None)
    window.navigate("settings")
    page = window.page("settings")
    assert page.check_updates.isChecked()
    page.check_updates.setChecked(False)
    page._save()
    assert window.ctx.settings.check_updates is False
    page.check_now.click()
    assert wait_until(qapp, lambda: not window.update_banner.isHidden(), 10)
    assert page.check_now.isEnabled()
