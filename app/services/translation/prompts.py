"""Instructions sent to the AI model. Changing them changes PROMPT_VERSION so
cached results made with older instructions are not reused."""

from __future__ import annotations

from app.services.translation.languages import Language

PROMPT_VERSION = "1"

_FIDELITY_RULES = """\
Rules:
- Translate the complete content. Do not summarize, shorten, explain, comment, censor or add notes.
- Do not omit anything and do not invent or add content that is not in the source.
- Preserve the meaning, tone, register, context and the order of sentences and lines of dialogue.
- Keep names and proper nouns appropriate for the target language (keep them, or transliterate them into the \
target script where that is customary). Keep numbers, units, URLs, e-mail addresses and code unchanged.
- Text that is already in the target language, or that has no translatable words (numbers, symbols), stays unchanged."""


def _source_phrase(source: Language | None) -> str:
    return f"from {source.name}" if source else "from the source language (detect it automatically)"


def text_system(target: Language, source: Language | None) -> str:
    return f"""\
You are a professional translator of books and documents. Translate every text segment {_source_phrase(source)} \
into {target.name}.
{_FIDELITY_RULES}
- Each segment is one paragraph, heading, caption or list item from a page. Translate it as one unit.
- A "\\n" inside a segment is an intentional line break (list items, verses, addresses): keep it.
- "context_before", when present, is the text that came just before, for continuity only. Do not translate it.
Answer with JSON: {{"translations": [{{"id": "<segment id>", "text": "<translation>"}}]}} containing exactly one \
entry for every input segment id, in the same order."""


_IMAGE_KINDS = {
    "comic": "a manga, comic or webtoon page",
    "scan": "a scanned page of a book or document",
}


def image_system(target: Language, source: Language | None, doc_type: str, include_sfx: bool) -> str:
    what = _IMAGE_KINDS.get(doc_type, "a page from a document, book, manga or comic")
    sfx = ("sound effects (onomatopoeia) drawn in the artwork" if include_sfx
           else "no sound effects (ignore onomatopoeia drawn in the artwork)")
    return f"""\
You are a professional translator of manga, comics, webtoons and books. The image is {what}. Find every piece of \
meaningful text on it and translate it {_source_phrase(source)} into {target.name}.
Include: speech and thought bubbles, narration boxes, captions, titles, signs, labels, notes, handwritten text, \
paragraphs of body text, and {sfx}. Skip page numbers, watermarks and scan-group credits.
For each text item return:
- "box_2d": [ymin, xmin, ymax, xmax] - a tight box around the original text itself, as integers normalized to \
0-1000 relative to the image height (y) and width (x).
- "kind": one of dialogue, thought, narration, caption, sign, sfx, text, other.
- "original": the text exactly as written in the image.
- "translation": the complete translation into {target.name}.
Make one item per speech bubble, box, caption or paragraph (not one per line). List items in natural reading order \
(right to left for Japanese manga, left to right otherwise). Use the pictures to understand who is speaking and what \
is happening, so the translation fits the scene.
{_FIDELITY_RULES}
- Only report text that is really visible in the image.
Answer with JSON: {{"regions": [...]}}. If there is no text, answer {{"regions": []}}."""


def image_user(target: Language) -> str:
    return f"Find all text in this page image and translate it into {target.name}."


def segments_user(payload_json: str) -> str:
    return "Translate the segments in this JSON:\n" + payload_json
