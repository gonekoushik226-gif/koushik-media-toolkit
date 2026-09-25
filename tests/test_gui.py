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
    from app.config.settings import Settings, SettingsStore
    from app.services.tools import ToolLocator
    from app.ui.context import AppContext
    from app.ui.main_window import MainWindow
    from app.ui.modules import build_registry

    ctx = AppContext(settings=Settings(output_dir=str(tmp_path / "out")), store=SettingsStore(tmp_path / "s.json"),
                     tools=None)
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
    assert not list(tmp_path.glob("*.webm")) and not list(tmp_path.glob("*kmt-partial*"))


def test_invalid_url_is_rejected(window, qapp, dialogs):
    window.navigate("video", "download")
    panel = window.page("video").panel("download")
    panel.url.setText("file:///C:/Windows/win.ini")
    panel.analyze()
    assert dialogs["error"] and not window.ctx.jobs.busy
