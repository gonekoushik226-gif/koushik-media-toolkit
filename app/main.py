"""Application start-up."""

from __future__ import annotations

import argparse
import logging
import platform
import sys

from app import APP_ID, APP_NAME, APP_PUBLISHER, __version__
from app.config import paths

log = logging.getLogger("app")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_ID, add_help=False)
    parser.add_argument("--self-test", action="store_true", help="run built-in checks and exit")
    parser.add_argument("--self-test-output", default="", help="write the self-test report (JSON) to this file")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--self-test-network", action="store_true", help="self-test: also check HTTPS and yt-dlp online")
    parser.add_argument("--self-test-download", default="", help="self-test: also download this link")
    args, _ = parser.parse_known_args(argv)  # never prints: the windowed EXE has no console
    return args


def _set_app_user_model_id() -> None:
    """Makes Windows group the taskbar button under our own icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_PUBLISHER}.{APP_ID}")
    except (AttributeError, OSError):
        pass


def _install_excepthook(window) -> None:
    """Log unexpected errors and show a friendly message instead of crashing."""
    from app.core.errors import describe_exception

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("app").critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        try:
            from app.ui.dialogs import show_error

            show_error(window, describe_exception(exc))
        except Exception:  # noqa: BLE001 - the hook itself must never fail
            pass

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        from app.selftest import run_self_test

        return run_self_test(args.self_test_output, args.self_test_network, args.self_test_download)

    from app.utils.logging_setup import setup_logging

    log_file = setup_logging(paths.log_dir(), debug=args.debug, console=not paths.is_frozen())
    log.info("Starting %s %s (%s, Python %s, %s %s)", APP_NAME, __version__,
             "packaged" if paths.is_frozen() else "source", platform.python_version(), platform.system(),
             platform.version())
    log.info("Log file: %s", log_file)
    _set_app_user_model_id()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from app.config.settings import SettingsStore
    from app.services.tools import ToolLocator
    from app.ui.context import AppContext
    from app.ui.main_window import MainWindow
    from app.ui.modules import build_registry
    from app.ui.theme import apply_theme

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_PUBLISHER)
    app.setApplicationVersion(__version__)
    icon = paths.asset_path("app.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    store = SettingsStore()
    settings = store.load()
    apply_theme(app, settings.theme)
    ctx = AppContext(settings=settings, store=store, tools=None)  # type: ignore[arg-type]
    ctx.tools = ToolLocator(lambda: ctx.settings)
    ctx.on_settings_changed(lambda: apply_theme(app, ctx.settings.theme))

    window = MainWindow(ctx, build_registry())
    _install_excepthook(window)
    window.show()
    code = app.exec()
    log.info("Exit code %s", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
