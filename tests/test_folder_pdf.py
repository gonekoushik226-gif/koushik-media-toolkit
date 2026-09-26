"""Image folder -> PDF: the PDF is named after the folder, pages in natural order."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app.core.jobs import JobContext
from app.services import pdf as pdf_service
from app.services.images import list_images_in_folder
from app.utils.filenames import build_filename


@pytest.mark.parametrize("folder, expected", [
    ("My Comic Chapter 01", "My Comic Chapter 01"),
    ("Chapter 10", "Chapter 10"),
    ("漫画 第1話", "漫画 第1話"),
    ("తెలుగు కథ", "తెలుగు కథ"),
    ("Café Crème", "Café Crème"),
    ("Vol. 2", "Vol. 2"),
    ("Bad: Name? <1>", "Bad_ Name_ _1_"),
    ("Trailing dots...", "Trailing dots"),
    ("CON", "_CON"),
])
def test_pdf_name_for_folder(folder, expected):
    assert pdf_service.pdf_name_for_folder(Path("C:/Comics") / folder) == expected


def test_long_folder_name_is_shortened():
    name = pdf_service.pdf_name_for_folder(Path("C:/x") / ("Very long chapter title " * 20))
    assert 0 < len(name) <= 180 and name.startswith("Very long chapter title")


def test_drive_root_falls_back():
    assert pdf_service.pdf_name_for_folder(Path("D:/")) == "D"


def _colored(folder: Path, names_colors) -> None:
    for name, color in names_colors:
        Image.new("RGB", (40, 30), color).save(folder / name)


def _page_colors(pdf: Path) -> list[tuple[int, int, int]]:
    with pymupdf.open(str(pdf)) as doc:
        return [tuple(page.get_pixmap(dpi=20).pixel(5, 5)) for page in doc]


def test_folder_to_pdf_named_after_folder_in_natural_order(tmp_path):
    folder = tmp_path / "My Comic Chapter 01"
    folder.mkdir()
    _colored(folder, [("10.png", (0, 0, 255)), ("2.png", (0, 255, 0)), ("1.png", (255, 0, 0))])
    images = list_images_in_folder(folder)
    assert [p.name for p in images] == ["1.png", "2.png", "10.png"]
    output = tmp_path / build_filename(pdf_service.pdf_name_for_folder(folder), "pdf")
    pdf_service.images_to_pdf([pdf_service.ImageSource(p) for p in images], output, "image", JobContext())
    assert output.name == "My Comic Chapter 01.pdf"
    colors = _page_colors(output)
    assert colors[0][0] > 200 and colors[1][1] > 200 and colors[2][2] > 200  # red, green, blue


def test_many_images(tmp_path):
    folder = tmp_path / "Big Chapter"
    folder.mkdir()
    for number in range(1, 321):
        Image.new("RGB", (16, 12), (number % 256, 0, 0)).save(folder / f"page {number}.jpg")
    images = list_images_in_folder(folder)
    assert [p.name for p in images[:3]] == ["page 1.jpg", "page 2.jpg", "page 3.jpg"] and len(images) == 320
    output = tmp_path / "Big Chapter.pdf"
    pdf_service.images_to_pdf([pdf_service.ImageSource(p) for p in images], output, "image", JobContext())
    assert pdf_service.count_pages(output) == 320
