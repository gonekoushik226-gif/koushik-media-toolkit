<p align="center">
  <img src="assets/app.png" width="96" alt="Koushik Media Toolkit icon">
</p>

<h1 align="center">Koushik Media Toolkit</h1>

<p align="center">
  One simple Windows app for everyday <b>video</b>, <b>audio</b>, <b>image</b> and <b>PDF</b> jobs.<br>
  Download from websites · trim · merge · convert · compress · crop · reorder · make, merge and split PDFs
</p>

<p align="center">
  <a href="../../releases/latest"><b>⬇ Download for Windows</b></a>
  &nbsp;·&nbsp; Windows 10 / 11 (64-bit)
  &nbsp;·&nbsp; Nothing else to install - FFmpeg and yt-dlp are included
</p>

<p align="center">
  <img src="docs/screenshots/home.png" width="800" alt="Home screen">
</p>

---

## Download

Go to the [**latest release**](../../releases/latest) and pick one file:

| File | Choose it if you want |
|---|---|
| `KoushikMediaToolkit-1.0.0-Setup.exe` (≈130 MB) | A normal installed app: Start Menu entry, optional desktop shortcut, uninstall from *Settings → Apps*. Opens in about a second. **Recommended.** |
| `KoushikMediaToolkit-1.0.0-Portable.exe` (≈180 MB) | A single file you can run from any folder or USB stick, without installing. Takes about 5 seconds to open. |

* The installer does **not** need administrator rights (it installs for your user; you can choose "all users" on the first page).
* **"Windows protected your PC"?** The app is not code-signed yet, so SmartScreen may warn the first time. Click **More info → Run anyway**.
* Each release includes `SHA256SUMS.txt` so you can check the download (`Get-FileHash <file>` in PowerShell).

---

## What it can do

### 🎬 Video
* **Download from a link** - YouTube and the [~1,800 sites supported by yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). Press *Analyze formats* to see every quality the site really offers (resolution, FPS, codecs, size, bitrate); pick one or keep *Best available*. When a quality has no sound, the best matching audio is added automatically.
* Video information · Trim · Merge · Extract audio · Remove audio · Extract the video stream · Convert (MP4, MKV, MOV, WEBM, AVI) · Resize · Rotate 90°/180° · Flip · Change speed (0.25×-4×) · Change volume · Compress

### 🎵 Audio / Music
* **Download audio from a link** - keep the original or convert to **MP3, M4A, WAV, FLAC or OPUS** at the bitrate you choose.
* Audio information · Trim · Merge · Extract audio from a video · Convert / change bitrate · Change volume · Fade in / fade out · Edit tags (title, artist, album, year, genre, track, ...)

### 🖼️ Images
* JPG, PNG, WEBP, BMP, TIFF, GIF and ICO - add files or a whole folder, or drag them in.
* Live preview · crop by dragging · resize · rotate · flip · brightness · contrast · convert with quality control · rename.
* Order the list by hand (drag, Move up / down) or sort by name, natural name (2 before 10), date taken, date created, date modified or size - then reverse or shuffle.
* Photos from phones are automatically turned the right way up. Your originals are never changed.

### 📄 PDF
* **Create a PDF from images** (JPEGs are embedded without quality loss) · **Merge PDFs** in any order
* **Split a PDF** into *N equal parts* (e.g. 500 pages → 10 files of 50) or *every N pages* (500 pages → 4 files of 125); the result is shown before you start
* **PDF to images** (PNG or JPEG, you choose the DPI) · **Download a PDF** from a direct link (checked to be a real PDF) · PDF information

<p align="center">
  <img src="docs/screenshots/video-download.png" width="49%" alt="Choosing a download quality">
  <img src="docs/screenshots/images.png" width="49%" alt="Editing images">
</p>
<p align="center">
  <img src="docs/screenshots/pdf-split.png" width="60%" alt="Splitting a PDF">
</p>

### Also
* Works in the background - the window never freezes; every task shows its progress and has a **Cancel** button.
* **Never leaves broken files**: results are written to a temporary file, checked, and only then saved under the real name. Existing files are only replaced if you agree.
* Light and dark theme (follows Windows by default).
* **Diagnostics** screen that checks every component and tells you how to fix problems.

---

## How to use it

1. Open the app and click a card: **VIDEO**, **AUDIO / MUSIC**, **IMAGES** or **PDF** (or **DOWNLOAD**).
2. Pick a task from the list on the left.
3. Choose your file (**Browse**, or drag it from Explorer) or paste a link.
4. Set the options, check the output folder and file name.
5. Press the blue button. Done - **Show file** opens the result in Explorer.

Press **Home** (top-left, or `Ctrl+H`) to go back.

**Settings** lets you choose the download folder, the default output folder, what happens when a file already exists (ask / keep both / replace), preferred video and audio formats and quality, and the theme.
Settings are stored in `%APPDATA%\KoushikMediaToolkit`, logs in `%LOCALAPPDATA%\KoushikMediaToolkit\logs`.
*Portable use:* put an empty file named `portable.txt` next to the portable EXE and everything is stored beside it instead.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| A website download stopped working | Websites change often. Download the latest `yt-dlp.exe` from [yt-dlp releases](https://github.com/yt-dlp/yt-dlp/releases) and select it in **Settings → Advanced**. It can then update itself with `yt-dlp -U`. |
| YouTube shows only a few formats | Install the free Deno runtime: `winget install DenoLand.Deno`, then restart the app. **Diagnostics** shows whether it was found. |
| "Sign in to confirm you're not a bot" / login needed | Downloads that need an account are not supported. Try again later. |
| "The link opened a web page instead of a PDF" | Open the link in your browser and copy the address of the PDF file itself. |
| "The file is being used by another program" | Close the program that has the file open (media player, PDF viewer). |
| Something else | Open **Diagnostics → Copy report**. Technical details are in the log folder. |

**Limitations:** content behind a login, paywall or DRM, live streams and playlists cannot be downloaded (on purpose). PDFs that need a password to open are not supported. For animated GIF/WEBP only the first frame is edited.

---

## Building from source

The complete source code is in this repository.

**Requirements:** Windows 10/11, Python 3.11+ (developed with 3.14), FFmpeg on the PATH (`winget install Gyan.FFmpeg`), and for the installer [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`).

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe run.py            # run the app
.venv\Scripts\python.exe -m pytest         # 300+ tests, no internet needed
.venv\Scripts\python.exe build.py          # portable EXE + installer -> release\
```

`build.py` runs the tests, bundles FFmpeg, builds the app with PyInstaller (folder + single-file EXE), runs a self-test on each packaged build, compiles the installer and writes checksums. A packaged app can be checked any time with
`KoushikMediaToolkit.exe --self-test --self-test-output report.json`.

### Project structure
```
app/
├─ main.py          start-up
├─ config/          settings and file locations
├─ core/            errors, background jobs (progress + cancel), module registry
├─ models/          media information and download formats
├─ services/        all the real work (FFmpeg, yt-dlp, images, PDF) - no GUI code
├─ ui/              PySide6 windows, pages and widgets
└─ utils/           file names, sorting, time formats, URLs, logging
tests/              automated tests
installer/          Inno Setup script
build.py            build script
docs/               developer guide and screenshots
```
New modules (e.g. subtitles, GIF tools, OCR) plug in without changing the rest of the app - see the **[developer guide](docs/DEVELOPER_GUIDE.md)**.

---

## License and credits

This project is licensed under the **GNU Affero General Public License v3.0** - see [LICENSE](LICENSE).
(The AGPL is required because the app includes PyMuPDF, which is AGPL-licensed.)

It is built on these excellent open-source projects:

| Component | Used for | License |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) (bundled GPL build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)) | video and audio processing | GPL-3.0 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | website downloads | Unlicense |
| [PySide6 / Qt](https://www.qt.io/qt-for-python) | user interface | LGPL-3.0 |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | PDF rendering | AGPL-3.0 |
| [pypdf](https://github.com/py-pdf/pypdf) | PDF merge and split | BSD-3-Clause |
| [Pillow](https://python-pillow.org) | images | MIT-CMU |
| [requests](https://requests.readthedocs.io) | PDF downloads | Apache-2.0 |

**Please only download content you have the right to download.** This app does not bypass DRM, paywalls, logins or any other access protection.
