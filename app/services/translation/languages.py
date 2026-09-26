"""Languages offered for translation (all are supported by the Gemini models)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    name: str  # English name - used in prompts and output file names
    native: str
    rtl: bool = False

    @property
    def label(self) -> str:
        return self.name if self.native == self.name else f"{self.name} ({self.native})"


LANGUAGES: tuple[Language, ...] = (
    Language("en", "English", "English"),
    Language("hi", "Hindi", "हिन्दी"),
    Language("te", "Telugu", "తెలుగు"),
    Language("ta", "Tamil", "தமிழ்"),
    Language("kn", "Kannada", "ಕನ್ನಡ"),
    Language("ml", "Malayalam", "മലയാളം"),
    Language("mr", "Marathi", "मराठी"),
    Language("bn", "Bengali", "বাংলা"),
    Language("gu", "Gujarati", "ગુજરાતી"),
    Language("pa", "Punjabi", "ਪੰਜਾਬੀ"),
    Language("or", "Odia", "ଓଡ଼ିଆ"),
    Language("ur", "Urdu", "اردو", rtl=True),
    Language("ne", "Nepali", "नेपाली"),
    Language("si", "Sinhala", "සිංහල"),
    Language("es", "Spanish", "Español"),
    Language("fr", "French", "Français"),
    Language("de", "German", "Deutsch"),
    Language("it", "Italian", "Italiano"),
    Language("pt-BR", "Portuguese (Brazil)", "Português (Brasil)"),
    Language("pt-PT", "Portuguese (Portugal)", "Português (Portugal)"),
    Language("nl", "Dutch", "Nederlands"),
    Language("sv", "Swedish", "Svenska"),
    Language("no", "Norwegian", "Norsk"),
    Language("da", "Danish", "Dansk"),
    Language("fi", "Finnish", "Suomi"),
    Language("pl", "Polish", "Polski"),
    Language("cs", "Czech", "Čeština"),
    Language("ro", "Romanian", "Română"),
    Language("hu", "Hungarian", "Magyar"),
    Language("el", "Greek", "Ελληνικά"),
    Language("ru", "Russian", "Русский"),
    Language("uk", "Ukrainian", "Українська"),
    Language("tr", "Turkish", "Türkçe"),
    Language("ar", "Arabic", "العربية", rtl=True),
    Language("he", "Hebrew", "עברית", rtl=True),
    Language("fa", "Persian", "فارسی", rtl=True),
    Language("id", "Indonesian", "Bahasa Indonesia"),
    Language("ms", "Malay", "Bahasa Melayu"),
    Language("fil", "Filipino", "Filipino"),
    Language("th", "Thai", "ไทย"),
    Language("vi", "Vietnamese", "Tiếng Việt"),
    Language("ja", "Japanese", "日本語"),
    Language("ko", "Korean", "한국어"),
    Language("zh-Hans", "Chinese (Simplified)", "简体中文"),
    Language("zh-Hant", "Chinese (Traditional)", "繁體中文"),
    Language("sw", "Swahili", "Kiswahili"),
)
_BY_CODE = {lang.code: lang for lang in LANGUAGES}
_BY_NAME = {lang.name.lower(): lang for lang in LANGUAGES}
DEFAULT_TARGET = "en"


def get_language(code_or_name: str | None) -> Language | None:
    if not code_or_name:
        return None
    return _BY_CODE.get(code_or_name) or _BY_NAME.get(code_or_name.strip().lower())
