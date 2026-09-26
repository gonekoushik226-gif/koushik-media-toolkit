"""TRANSLATE module: AI translation of PDFs and image folders.

Every user uses their OWN API key (Google Gemini API). The key is entered on
the "AI provider & API key" panel and kept in Windows Credential Manager; it is
never part of the application, the settings file or the log.
"""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.errors import AppError, InvalidInputError
from app.services.images import IMAGE_EXTENSIONS
from app.services.translation import gemini
from app.services.translation import job as translation_job
from app.services.translation.languages import LANGUAGES, get_language
from app.ui.dialogs import confirm, show_error, show_info
from app.ui.jobs import run_in_background
from app.ui.pages.base import OperationPanel, OperationsPage
from app.ui.widgets.common import combo, hint, label, set_bold
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.output_panel import OutputPanel
from app.utils.filenames import same_path

INPUT_FILTER = "PDF files (*.pdf);;All files (*.*)"


def link_label(text_html: str, name: str = "") -> QLabel:
    widget = QLabel(text_html)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.RichText)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    widget.setOpenExternalLinks(True)
    if name:
        widget.setObjectName(name)
    return widget


def _link(url: str, text: str | None = None) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(text or url)}</a>'


SETUP_STEPS = f"""
<ol style="margin-left: -18px">
<li>Open Google AI Studio: {_link(gemini.KEY_PAGE_URL)}</li>
<li>Sign in with your Google account. If you do not have one, choose <i>Create account</i> on the sign-in page.</li>
<li>If Google asks you to accept the Gemini API terms, read and accept them.</li>
<li>On the <i>API Keys</i> page, copy your key. New users usually already have one (Google creates a default
project and key after the terms are accepted); otherwise click <b>Create API key</b>. If you already use Google
Cloud, you may first have to choose or import a project.</li>
<li>Paste the key into the box below, click <b>Test key</b> and then <b>Save key</b>.</li>
</ol>
Google's official guide: {_link(gemini.KEY_DOCS_URL)}
"""

COST_NOTE = f"""
The requests are made with <b>your</b> key, so they count against <b>your</b> Google account's limits.
Google offers a free tier with daily and per-minute limits; if you turn on billing for your key's project,
any charges go to your own Google account - never to the makers of this app.
Pricing: {_link(gemini.PRICING_URL)} &nbsp;·&nbsp; Your current limits: {_link(gemini.RATE_LIMIT_URL)}<br>
<b>Privacy:</b> the pages you translate are sent to Google. According to Google's terms, content sent with an
unpaid (free tier) key may be used to improve Google's products, so do not translate confidential documents
with a free-tier key.
"""


def missing_key_message() -> str:
    return ("AI translation uses your own free Google Gemini API key, and no key is set up yet.\n\n"
            "Open 'AI provider & API key' (the button below) and follow the steps there: create a key in "
            "Google AI Studio, paste it, test it and save it. It takes about two minutes.")


def ask_to_set_up_key(parent, ctx) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, "API key needed", missing_key_message(),
                      QMessageBox.StandardButton.NoButton, parent)
    setup = box.addButton("Set up API key", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is setup:
        ctx.navigate("translate", "key")


def _language_combo(current: str, with_auto: bool) -> QComboBox:
    items = {"": "Detect automatically"} if with_auto else {}
    items.update({lang.code: lang.label for lang in LANGUAGES})
    return combo(items, current)


class TranslatePanel(OperationPanel):
    key = "translate"
    title = "Translate a document"
    description = ("Translate a PDF - a book, document, scanned pages, manga, comic or webtoon - or a folder of "
                   "comic images into another language. A new PDF is created; the original is never changed. "
                   "Text pages keep their layout; on image pages the original lettering is covered and the "
                   "translation is written into the same bubble or box.")
    action_text = "Translate"

    def build(self) -> None:
        # -- provider / key status ------------------------------------------------
        status_group = QGroupBox("AI provider")
        status_box = QVBoxLayout(status_group)
        self.provider_label = label("", "muted")
        self.key_status = label("")
        row = QHBoxLayout()
        row.addWidget(self.key_status, 1)
        self.key_button = QPushButton("API key...")
        self.key_button.clicked.connect(lambda: self.ctx.navigate("translate", "key"))
        row.addWidget(self.key_button)
        status_box.addWidget(self.provider_label)
        status_box.addLayout(row)
        self.body.addWidget(status_group)

        # -- input ---------------------------------------------------------------------
        group = QGroupBox("PDF file or folder of images")
        box = QVBoxLayout(group)
        self.picker = FilePicker(self.ctx, (".pdf",), INPUT_FILTER, "Choose a PDF or a folder of images...",
                                 allow_folder=True)
        self.picker.changed.connect(self._input_changed)
        self.input_info = label("", "muted")
        box.addWidget(self.picker)
        box.addWidget(self.input_info)
        self.body.addWidget(group)
        self.plan: translation_job.DocumentPlan | None = None

        # -- options ---------------------------------------------------------------------
        s = self.ctx.settings
        _, form = self.form_group("Translation")
        self.doc_type = combo(translation_job.DOC_TYPES, s.translation_doc_type)
        self.doc_type.currentIndexChanged.connect(lambda _: self._analyze())
        form.addRow("Document type:", self.doc_type)
        self.source_language = _language_combo(s.translation_source, with_auto=True)
        form.addRow("From:", self.source_language)
        self.target_language = _language_combo(s.translation_target, with_auto=False)
        self.target_language.currentIndexChanged.connect(lambda _: self._suggest_name())
        form.addRow("To:", self.target_language)
        self.page_range = QLineEdit()
        self.page_range.setPlaceholderText("All pages - or for example 1-5, 8, 10-12")
        form.addRow("Pages:", self.page_range)
        self.sfx = QCheckBox("Translate sound effects in comics (shown as small labels; the artwork is kept)")
        self.sfx.setChecked(s.translation_sfx)
        form.addRow("", self.sfx)
        form.addRow("", hint("Every page is sent to the AI service with your API key and counts toward your own "
                             "usage limits. Pages that are already translated are remembered: if a run stops (limit "
                             "reached, no internet), run it again and it continues where it stopped."))

        # -- output ----------------------------------------------------------------------
        self.output = OutputPanel(self.ctx)
        self.output.set_extension("pdf")
        self.output.suggest_folder(self.ctx.output_dir_for(None))
        self.body.addWidget(self.output)
        self.refresh_status()

    # -- status ------------------------------------------------------------------
    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh_status()

    def refresh_status(self) -> None:
        manager = self.ctx.key_manager()
        self.provider_label.setText(f"Provider: {gemini.PROVIDER_NAME}  ·  Model: {self.ctx.translation_model()}")
        if manager.key():
            self.key_status.setText("✓ " + manager.status())
            self.key_status.setObjectName("successText")
            self.key_button.setText("Change API key...")
        else:
            self.key_status.setText("⚠ No API key yet. AI translation needs your own free Google Gemini API key - "
                                    "click 'Set up API key' to see how to get one.")
            self.key_status.setObjectName("warningText")
            self.key_button.setText("Set up API key...")
        self.key_status.style().unpolish(self.key_status)
        self.key_status.style().polish(self.key_status)

    # -- input ---------------------------------------------------------------------
    def target(self):
        return get_language(self.target_language.currentData())

    def _suggest_name(self) -> None:
        source = self.picker.path()
        if source is not None:
            self.output.suggest_name(translation_job.default_output_name(source, self.target()))

    def _input_changed(self, path: Path | None) -> None:
        self.plan = None
        self.input_info.setText("")
        if path is None:
            return
        if path.is_dir() and path.suffix.lower() != ".pdf":
            self._select_data(self.doc_type, "comic")
        self.output.suggest_folder(self.ctx.output_dir_for(path))
        self.output.suggest_name(translation_job.default_output_name(path, self.target()), force=True)
        self._analyze()

    @staticmethod
    def _select_data(box: QComboBox, value) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def _analyze(self) -> None:
        source = self.picker.path() if hasattr(self, "picker") else None
        if source is None:
            return
        self.plan = None
        self.input_info.setText("Reading the document...")
        token = object()
        self._token = token
        doc_type = self.doc_type.currentData()

        def done(plan) -> None:
            if self._token is token:
                self.plan = plan
                self.input_info.setText(f"{plan.total_pages} page(s): {plan.describe()}")

        def failed(exc: BaseException) -> None:
            if self._token is token:
                self.input_info.setText(f"⚠ {exc.message if isinstance(exc, AppError) else exc}")

        run_in_background(lambda: translation_job.analyze_source(source, doc_type), done, failed, owner=self)

    # -- run -------------------------------------------------------------------------
    def run(self) -> None:
        source = self.picker.path()
        if source is None:
            raise InvalidInputError("Please choose a PDF file or a folder of images first.")
        if source.is_dir() and not any(p.suffix.lower() in IMAGE_EXTENSIONS for p in source.iterdir()):
            raise InvalidInputError("The chosen folder does not contain any images.")
        if not self.ctx.key_manager().key():
            ask_to_set_up_key(self.window(), self.ctx)
            return
        if self.plan is None:
            raise InvalidInputError("The document is still being read (or could not be read). Please wait a moment "
                                    "or choose another file.")
        pages = translation_job.parse_page_range(self.page_range.text(), self.plan.total_pages)
        count = len(pages) if pages is not None else self.plan.total_pages
        target = self.target()
        source_language = self.source_language.currentData()
        source_lang = get_language(source_language) if source_language else None
        if source_lang is not None and source_lang.code == target.code:
            raise InvalidInputError(f"The document is already set to be in {target.name}. Choose another target "
                                    "language.")
        output = self.output.output_path()
        if same_path(output, source):
            raise InvalidInputError("The translated PDF cannot replace the original. Choose another file name.")
        if count > translation_job.LARGE_DOCUMENT_PAGES and not confirm(
                self.window(), "Large document",
                f"You are about to translate {count} pages. This can take a long time and uses a lot of your API "
                "quota (and costs money if billing is enabled for your key).\n\nTip: use 'Pages' to translate a "
                "part first.\n\nContinue?", "Translate"):
            return
        final = self.confirm_output(output)
        if final is None:
            return
        doc_type = self.doc_type.currentData()
        s = self.ctx.settings
        s.translation_target, s.translation_source = target.code, source_language
        s.translation_doc_type, s.translation_sfx = doc_type, self.sfx.isChecked()
        self.ctx.save_settings()
        provider = self.ctx.translation_provider()
        request = translation_job.TranslationRequest(
            source=source, output=final, target=target, source_language=source_lang, doc_type=doc_type,
            pages=pages, include_sfx=self.sfx.isChecked(), model=getattr(provider, "model", ""))
        self.start_job(f"Translating {source.name} into {target.name}",
                       lambda ctx: translation_job.translate_document(request, provider, ctx),
                       reveal=self.output.reveal_when_done())


class ApiKeyPanel(OperationPanel):
    key = "key"
    title = "AI provider & API key"
    description = (f"AI translation uses the {gemini.PROVIDER_NAME} with your own API key. The app does not include "
                   "a key of its own: each person uses their own key, so usage limits and any charges belong to "
                   "their own Google account. Your key is only sent to Google.")
    action_text = "Save key"

    def build(self) -> None:
        steps = QGroupBox("How to get your own API key")
        steps_box = QVBoxLayout(steps)
        steps_box.addWidget(link_label(SETUP_STEPS))
        self.body.addWidget(steps)

        costs = QGroupBox("Costs, limits and privacy")
        costs_box = QVBoxLayout(costs)
        costs_box.addWidget(link_label(COST_NOTE))
        self.body.addWidget(costs)

        key_group = QGroupBox("Your API key")
        key_box = QVBoxLayout(key_group)
        self.status = label("")
        set_bold(self.status)
        key_box.addWidget(self.status)
        row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your API key here")
        self.key_edit.setClearButtonEnabled(True)
        row.addWidget(self.key_edit, 1)
        self.show_key = QCheckBox("Show")
        self.show_key.toggled.connect(lambda on: self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row.addWidget(self.show_key)
        key_box.addLayout(row)
        buttons = QHBoxLayout()
        self.test_button = QPushButton("Test key")
        self.test_button.setToolTip("Check the pasted key (or the saved key) with Google")
        self.test_button.clicked.connect(self.test_key)
        self.session_button = QPushButton("Use for this session only")
        self.session_button.setToolTip("Use the pasted key until the app is closed, without saving it")
        self.session_button.clicked.connect(self.use_for_session)
        self.remove_button = QPushButton("Remove saved key")
        self.remove_button.clicked.connect(self.remove_key)
        # "Save key" (the panel's main button) sits next to the key box instead of at the bottom of the page.
        self.action_row.removeWidget(self.action_button)
        for button in (self.test_button, self.action_button, self.session_button, self.remove_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        key_box.addLayout(buttons)
        self.test_result = label("", "muted")
        key_box.addWidget(self.test_result)
        key_box.addWidget(hint("Saved keys are kept in Windows Credential Manager, protected by your Windows "
                               "account (Control Panel > Credential Manager > Windows Credentials). The key is never "
                               "written to the settings file or the log, and the full key is never shown again."))
        self.body.addWidget(key_group)

        model_group, form = self.form_group("AI model")
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for model_id in gemini.RECOMMENDED_MODELS:
            self.model.addItem(model_id, model_id)
        self.model.setCurrentText(self.ctx.translation_model())
        self.model.currentTextChanged.connect(lambda _: self._model_changed())
        refresh = QPushButton("Refresh list")
        refresh.setToolTip("Ask Google which models your key can use")
        refresh.clicked.connect(lambda: self.test_key(update_models_only=True))
        row = QHBoxLayout()
        row.addWidget(self.model, 1)
        row.addWidget(refresh)
        form.addRow("Model:", self._wrap_row(row))
        form.addRow("", hint(f"Recommended: {gemini.RECOMMENDED_MODELS[0]} (best quality for the price) or "
                             f"{gemini.RECOMMENDED_MODELS[1]} (faster and cheaper). Newer models appear after "
                             "'Refresh list'."))
        self.refresh_status()

    @staticmethod
    def _wrap_row(layout):
        from PySide6.QtWidgets import QWidget

        widget = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        return widget

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh_status()

    def refresh_status(self) -> None:
        manager = self.ctx.key_manager()
        self.status.setText(manager.status())
        self.remove_button.setEnabled(manager.is_saved())

    def model_id(self) -> str:
        """The chosen model: a list entry's id, or what the user typed."""
        text = self.model.currentText().strip()
        index = self.model.findText(text)
        data = self.model.itemData(index) if index >= 0 else None
        return str(data or text).removeprefix("models/")

    def _model_changed(self) -> None:
        value = self.model_id()
        if value and value != self.ctx.translation_model():
            self.ctx.settings.translation_model = "" if value == gemini.DEFAULT_MODEL else value
            self.ctx.save_settings()

    # -- actions -------------------------------------------------------------------
    def run(self) -> None:  # "Save key"
        manager = self.ctx.key_manager()
        replacing = manager.is_saved()
        manager.save(self.key_edit.text())
        self.key_edit.clear()
        self.show_key.setChecked(False)
        self.refresh_status()
        show_info(self.window(), "API key saved",
                  ("Your new API key replaced the old one." if replacing else "Your API key was saved.")
                  + " You can now translate documents.")

    def use_for_session(self) -> None:
        try:
            self.ctx.key_manager().use_for_session(self.key_edit.text())
        except AppError as exc:
            show_error(self.window(), exc)
            return
        self.key_edit.clear()
        self.refresh_status()

    def remove_key(self) -> None:
        if not confirm(self.window(), "Remove API key", "Remove the saved API key from this computer?\n\n"
                       "The key itself stays valid at Google; to revoke it, delete it in Google AI Studio.", "Remove"):
            return
        try:
            self.ctx.key_manager().remove()
        except AppError as exc:
            show_error(self.window(), exc)
            return
        self.test_result.setText("")
        self.refresh_status()

    def test_key(self, update_models_only: bool = False) -> None:
        typed = self.key_edit.text().strip()
        try:
            if typed:
                from app.services.translation.keys import validate_key_format

                typed = validate_key_format(typed)
            provider = self.ctx.translation_provider(api_key=typed or None, model=self.model_id())
        except AppError as exc:
            if not typed and not self.ctx.key_manager().key():
                self.test_result.setText("Paste your API key first (or save one), then test it.")
            else:
                show_error(self.window(), exc)
            return
        if hasattr(provider, "max_retries"):
            provider.max_retries = 1  # a quick answer is better than a long wait here
        self.test_button.setEnabled(False)
        self.test_result.setText("Contacting Google...")
        wanted = self.model_id()

        def done(models) -> None:
            self.test_button.setEnabled(True)
            self.model.blockSignals(True)
            self.model.clear()
            for info in models:
                self.model.addItem(info.label, info.id)
            index = self.model.findData(wanted)
            if index >= 0:
                self.model.setCurrentIndex(index)
            else:
                self.model.setCurrentText(wanted)
            self.model.blockSignals(False)
            ids = {m.id for m in models}
            text = f"✓ The key works. {len(models)} suitable model(s) are available."
            if wanted and ids and wanted not in ids:
                text += f" Note: '{wanted}' is not one of them - choose a model from the list."
            if update_models_only:
                text = f"Model list updated ({len(models)} models)."
            self.test_result.setText(text)

        def failed(exc: BaseException) -> None:
            self.test_button.setEnabled(True)
            if isinstance(exc, AppError):
                self.test_result.setText(f"✗ {exc.title}: {exc.message}")
            else:
                self.test_result.setText("✗ The key could not be tested. See the log for details.")

        run_in_background(provider.list_models, done, failed, owner=self)


def build_translate_page(ctx) -> OperationsPage:
    operations = [
        ("translate", "Translate document", TranslatePanel),
        ("key", "AI provider & API key", ApiKeyPanel),
    ]
    return OperationsPage(ctx, operations)
