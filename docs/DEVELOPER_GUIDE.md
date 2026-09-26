# Developer guide - Media Toolkit

This document explains how the application is put together, the rules the code follows, and
how to extend it. For installation, usage and build commands see the [README](../README.md).

## 1. Layers

```
 ┌──────────────────────────── app/ui (PySide6) ─────────────────────────────┐
 │ MainWindow ─ HomePage ─ module pages ─ OperationPanels ─ widgets           │
 │        │ collects options on the GUI thread, never does heavy work         │
 │        ▼                                                                    │
 │ JobController ── QThread ──► service function(ctx: JobContext) ──┐         │
 │   ▲ status / progress / result / error signals (queued to GUI)   │         │
 └───┼──────────────────────────────────────────────────────────────┼─────────┘
     │                                                              ▼
 ┌───┴──────────────── app/services (no Qt) ─────────────────────────────────┐
 │ video · audio · images · pdf · downloads(ytdlp, plans, http) · diagnostics │
 │ ffmpeg/runner · ffmpeg/probe · ffmpeg/processor · ffmpeg/codecs · tools    │
 │ translation (provider · gemini · job · text_pages · image_pages · cleanup) │
 └───────────────┬──────────────────────────────────────┬─────────────────────┘
                 ▼                                      ▼
        app/models (data)                     app/core, app/utils, app/config
```

* **`app/services` never imports Qt.** Everything there can be called from tests, scripts or a
  future command-line front end.
* **`app/ui` never does slow work on the GUI thread.** It validates input, asks about
  overwriting, then starts a job. Small helper work (reading a file's length, loading a
  preview/thumbnail, counting PDF pages) uses `run_in_background` in `app/ui/jobs.py`.
* **`app/models`** are plain dataclasses and pure parsing functions (ffprobe JSON → `MediaInfo`,
  yt-dlp info dict → `RemoteMedia`).

## 2. Jobs, progress, cancellation

`app/core/jobs.py` defines `JobContext`, which every long-running service function receives:

| Method | Purpose |
|---|---|
| `set_status(text)` | Main status line ("Trimming video (fast copy)"). |
| `set_progress(fraction or None, detail)` | 0..1, or `None` for "busy, amount unknown". Throttled to ~10/s. |
| `step(done, total, detail)` | Countable work ("Page 3 / 10"). |
| `check_cancelled()` | Raises `JobCancelled` if the user pressed Cancel. Call it at safe points. |
| `add_cancel_callback(fn)` | Run `fn` immediately on Cancel (e.g. kill FFmpeg). Returns a remover. |
| `resolve_outputs(paths)` | Ask what to do with outputs that already exist (multi-file operations). |

`app/ui/jobs.py` wraps this in `JobController`: one user-visible job at a time on a `QThread`,
signals queued to the GUI thread, the status area updated, `JobResult.warnings` shown after
success, errors turned into friendly dialogs via `describe_exception`, and `resolve_outputs`
answered by a dialog on the GUI thread while the worker waits (cancel-aware).

`run_abandonable(fn, ctx)` runs a call that cannot be interrupted (yt-dlp's analysis) in a helper
thread, so Cancel can stop waiting immediately.

## 3. Errors

`app/core/errors.py`:

* `AppError(message, details, title)` - message for the user, `details` for the "Show Details"
  section (e.g. the last FFmpeg lines). Subclasses: `InvalidInputError`, `DependencyError`,
  `ProcessingError`, `DownloadError`.
* `JobCancelled` - not an error; shown as "Cancelled".
* `describe_exception(exc)` converts anything (including `PermissionError`, disk full, file in
  use, `MemoryError`) into an `ErrorReport`. Unexpected exceptions get a generic message; the
  traceback only goes to the log. Users never see a Python traceback.

Rule: raise `AppError` subclasses with plain-language messages for everything a user can cause
or fix. Let genuine bugs propagate - they are logged with a traceback and reported generically.

## 4. Safe outputs (the "never a corrupt file" rule)

* FFmpeg jobs go through `MediaProcessor.render` (`app/services/ffmpeg/processor.py`): output to
  `name.mt-partial-XXXX.ext` next to the destination, verify with ffprobe (`Expectation`:
  required/forbidden streams, duration within tolerance), then `os.replace` to the final name.
  On any failure or cancel the temp file is deleted.
* `render_first_working` tries a list of `Attempt`s - typically stream copy first, re-encode
  second - so lossless paths are used when they work and quality is never silently degraded
  into a broken file.
* Images and PDFs are written the same way (`save_image`, `_replace_from_temp`), PDFs are
  re-opened and page-counted after writing.
* Downloads go into a private `.mt-download-XXXX` folder inside the output folder and are moved
  into place at the end; the folder is always removed (with retries - Windows keeps files locked
  briefly after a cancelled download).
* Overwrite policy (`Settings.overwrite_policy`): single-output panels call
  `OperationPanel.confirm_output(path)` before starting; multi-output services call
  `ctx.resolve_outputs(paths)`. Without a UI resolver the default is "keep both", never replace.

## 5. External programs

`app/services/tools.py` finds FFmpeg/FFprobe in this order: path in Settings → copy bundled in the
package (`sys._MEIPASS/tools`) → `tools\` or the EXE folder → system PATH (reported as such in
Diagnostics). JavaScript runtimes for yt-dlp's YouTube support (deno, node, bun, quickjs) are
auto-detected.

All subprocesses use argument lists (never `shell=True`), `CREATE_NO_WINDOW`, and `stdin=DEVNULL`.
URLs are validated (`http`/`https` only) and passed to yt-dlp after `--` so they can never be read
as options. FFmpeg filter strings only contain numbers produced by the code; user text (tags) is
passed as separate `-metadata key=value` arguments.

## 6. yt-dlp integration

* `EmbeddedYtDlp` uses the bundled library; `ExternalYtDlp` runs a user-selected `yt-dlp.exe`
  (so users can update yt-dlp independently with `yt-dlp -U`). Both return the same
  `RemoteMedia` because the external backend parses `yt-dlp -J`, which is the same info dict.
* Analysis uses the format selector `bv*+ba/b/bv*/ba*`, so yt-dlp's own "best" choice (which
  respects original-language audio) is available as `RemoteMedia.default_format_ids`.
* `app/services/downloads/plans.py` turns the user's choice into an exact request: explicit
  `video+audio` ids, audio pairing (`pick_audio_for_video`), container choice
  (`choose_container`), audio conversion target, expected extension. It is pure and fully tested.
* Progress hooks feed `JobContext`; Cancel raises `DownloadCancelled` inside the hook (embedded) or
  kills the process tree (external). Errors are translated by `explain_ytdlp_error`.
* Never add cookie/login/DRM options. The app deliberately does not support them.

## 7. Settings, paths, logging

* `app/config/paths.py` is the only place that knows about frozen vs. source, portable mode
  (`portable.txt` next to the EXE) and `MEDIA_TOOLKIT_DATA_DIR` (tests).
* `app/config/settings.py`: a dataclass persisted as JSON; loading validates every value and
  never fails; saving is atomic. Add a setting by adding a field (with a default) and, for
  choices/ranges, an entry in `CHOICES`/`RANGES`, then a widget in `app/ui/pages/settings.py`.
* `app/utils/logging_setup.py`: rotating file log; every message passes through `redact()`,
  which strips URL query strings, credentials in URLs, cookies, passwords, tokens and anything
  that looks like a Google API key.
* `app/config/credentials.py`: secrets (the user's AI API key) live in **Windows Credential
  Manager** (`CredWriteW`/`CredReadW`/`CredDeleteW` via ctypes, target `MediaToolkit/<name>`),
  never in `settings.json`. `mask_secret()` gives the only form of a key that may be displayed or
  logged. Tests use `MemorySecretStore`.

## 8. UI building blocks

| Class | Use |
|---|---|
| `OperationPanel` (`ui/pages/base.py`) | One task: title, description, options in `self.body`, primary button calling `run()`. Raise `AppError` in `run()` for bad input. |
| `SingleFilePanel` | One input → one output. Override `build_options()`, `output_extension()`, `suggested_stem()`, `make_job(source)` (validate, return `fn(output, ctx)`), optionally `on_media_info()` (called after ffprobe). |
| `InfoPanel` | Shows text information about a file (`describer()`). |
| `OperationsPage` | Module page: list of `(key, label, PanelClass)`; panels are created lazily; `show_operation(key)` for deep links (`ctx.navigate("video", "download")`). |
| `FileListWidget` | Ordered file list: add files/folder, drag & drop (also from Explorer), remove, clear, move up/down, sort (name, natural, EXIF date, created, modified, size), reverse, shuffle. |
| `FilePicker`, `OutputPanel` | Single input file (optionally a folder: `allow_folder=True`); output folder + name + extension + "open folder when done". |
| `FormatTable`, `ImagePreview`, `StatusArea` | yt-dlp formats; image preview with crop selection; the status bar. |

Themes live in `ui/theme.py` (colour tokens → palette + style sheet). Icons are SVGs in
`assets/icons` using `currentColor`, tinted at runtime by `ui/icons.py`.

## 9. Adding a module (example: "Subtitles")

1. **Service** - `app/services/subtitles.py`, no Qt:
   ```python
   def burn_in(tools: MediaTools, video: Path, subtitle: Path, output: Path, ctx: JobContext) -> JobResult:
       processor = MediaProcessor(tools)
       info = processor.probe(video)
       attempt = Attempt("encoding", lambda tmp: ["-i", str(video), "-vf", f"subtitles={...}", ..., str(tmp)],
                         Expectation(video=True, duration=info.best_duration))
       processor.render_first_working(output, [attempt], ctx, "Adding subtitles")
       return JobResult(f"Saved {output.name}", outputs=[output])
   ```
   Add tests in `tests/test_subtitles.py` (use the `sample_video` fixture).
2. **Panels** - `app/ui/pages/subtitles.py`:
   ```python
   class BurnInPanel(SingleFilePanel):
       title = "Burn in subtitles"; action_text = "Add subtitles"; output_suffix = "_subtitled"
       input_extensions = VIDEO_EXTS; input_filter = VIDEO_FILTER
       def build_options(self): ...  # e.g. a FilePicker for the .srt file
       def make_job(self, source):
           tools, subs = self.ctx.tools.media_tools(), self.subs.path()
           if subs is None:
               raise InvalidInputError("Please choose a subtitle file.")
           return lambda output, ctx: burn_in(tools, source, subs, output, ctx)

   def build_subtitles_page(ctx):
       return OperationsPage(ctx, [("burn", "Burn in subtitles", BurnInPanel)])
   ```
3. **Register** - in `app/ui/modules.py`:
   ```python
   ModuleSpec("subtitles", "SUBTITLES", "Add or extract subtitles.", "video", _subtitles, GROUP_MAIN, 50)
   ```
   (add an SVG to `assets/icons` for a custom icon). The home screen and navigation pick it up.
4. Run `pytest`, `ruff check app tests`, then `build.py` - the packaged self-test opens every
   registered page, so a broken page fails the build.

## 10. AI translation (`app/services/translation`)

**Keys.** There is no developer key. Each user enters their own key (`keys.ApiKeyManager`:
save / replace / remove / session-only, backed by `credentials.py`). `AppContext.translation_provider()`
builds the provider with that key and raises `MissingApiKeyError` when there is none; the UI then
shows the setup steps. Never put a real key in code, tests, docs or example files - use
`YOUR_API_KEY_HERE`, and build fake keys in tests at run time (`"AIza" + "x" * 35`) so that secret
scanners do not flag them.

**Provider interface** (`provider.py`): `list_models()`, `translate_segments(segments, target,
source, context, ctx) -> {id: text}` and `analyze_image(image, mime, target, source, doc_type,
include_sfx, ctx) -> [{"box", "kind", "original", "translation"}]` (boxes normalised 0-1,
`x0, y0, x1, y1`). Errors are split into *fatal* ones (`FATAL_ERRORS`: key, quota, rate limit,
network, service, model - the job stops, finished pages stay cached) and `PageError`s (blocked,
unusable answer, rejected - only that page is skipped and listed as a warning).

**Gemini** (`gemini.py`): REST via `requests` (no extra dependency). The key goes only in the
`x-goog-api-key` header, never in a URL. JSON output is enforced with `responseSchema` (with a
fallback when a model refuses the schema). Retries: at most `max_retries` (default 4) for
network errors, 5xx and per-minute 429s, waiting for the server's `retryDelay` (capped at 90 s)
or exponential backoff; daily-quota 429s, 4xx and key errors are never retried. Missing segment
ids are asked for once more; an unusable answer is retried once.

**Job** (`job.py`): `plan_document()` decides per page - `text` (enough visible text, not
rotated) or `image` (scans, comics, invisible OCR layers, rotated pages); a folder of images is
first turned into a temporary PDF. Text pages are batched (`BATCH_CHARS`) with a little
preceding text for context; a batch that fails is retried page by page, and a too-long answer is
split in half. Each finished page is written to `ProgressCache` (in the data folder, keyed by a
hash of the document, language, model and options; it never contains the key), so a stopped run
resumes without repeating requests. The output is written to a temporary file, checked and then
renamed; the source file is only ever opened for reading, and an output path equal to the source
is refused.

**Pages.** `text_pages.py` removes each text block with a redaction that keeps images and vector
graphics, then writes the translation into the same rectangle with `insert_htmlbox` (PyMuPDF's
built-in Noto fonts shape Indic, CJK, Arabic, ...; the font size shrinks until it fits).
`image_pages.py` renders the page (tall webtoon strips as overlapping tiles), asks the AI for
text regions, and `cleanup.py` erases the lettering: flood-fill from around the text finds the
enclosing bubble (letters are the holes in it), which is repainted in its own colour as a
transparent PNG patch; text on artwork gets a small patch in the surrounding colour; sound
effects keep the artwork and get a label. The translation is then inserted, centred, inside the
bubble.

**Prompts** (`prompts.py`) state the fidelity rules (no summaries, comments, omissions or
additions; keep names, numbers and order). Bump `PROMPT_VERSION` when prompts change so cached
pages from older prompts are not reused.

**Adding another AI service:** implement `TranslationProvider` (map its errors to the classes in
`provider.py`), then choose it in `AppContext.translation_provider()` and add its key name and
setup steps to the key panel (`app/ui/pages/translate.py`).

## 11. Testing strategy

* Pure logic (split math, sorting, file names, time parsing, format parsing, download plans,
  URL/Content-Disposition handling) - plain unit tests.
* Real FFmpeg on generated clips (`tests/conftest.py` fixtures) - every video/audio operation,
  output verified with ffprobe; cancellation leaves no files.
* Downloads - a local `http.server` fixture serves PDFs, HTML, errors, truncated data and a DASH
  stream generated by FFmpeg; both yt-dlp backends are tested against it. No live websites.
* GUI - offscreen Qt; panels are driven through their buttons and jobs run through the real
  `JobController`.
* AI translation - no network and no real key: `tests/translation_helpers.FakeProvider` for the
  pipeline (text PDFs in several scripts, synthetic manga pages, webtoon tiling, resume, failures,
  cancel, 120-page batching, original file unchanged), a local mock HTTP server for the Gemini
  client (request shape, every error mapping, retries, schema fallback), and a round trip through
  the real Windows Credential Manager under a throw-away name.
* Packaged builds - `--self-test` (see `app/selftest.py`), run automatically by `build.py`.

## 12. Packaging notes

* PyInstaller `--onedir` for the installer (fast start), `--onefile` for the portable EXE.
* `ffmpeg.exe`/`ffprobe.exe` are added as *data* into `tools/` (skips PyInstaller's binary
  analysis of 100+ MB files). `yt_dlp_ejs` data and `certifi`'s CA bundle are collected explicitly.
* Unused Qt modules are excluded; only `PySide6-Essentials` is installed.
* The Inno Setup script installs per user by default (no admin), offers all-users install,
  removes the previous `_internal` folder on upgrade, and never touches user settings.
