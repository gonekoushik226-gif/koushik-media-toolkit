from pathlib import Path

import pytest
from PIL import Image

from app.core.errors import InvalidInputError, ProcessingError
from app.core.jobs import JobContext
from app.services import images as svc


def pixel(img, xy):
    value = img.getpixel(xy)
    return value[:3] if isinstance(value, tuple) else value


BLUE = (0, 0, 255)
RED = (200, 30, 30)


class TestEditPipeline:
    @pytest.fixture
    def img(self, image_factory):
        # 64x48, blue 8x8 marker in the top-left corner
        return Image.open(image_factory("a.png")).convert("RGB")

    def test_rotate_clockwise_moves_marker_to_top_right(self, img):
        out = svc.apply_edits(img, svc.ImageEdits([svc.rotate_op(90)]))
        assert out.size == (48, 64) and pixel(out, (47, 0)) == BLUE

    def test_rotate_180(self, img):
        out = svc.apply_edits(img, svc.ImageEdits([svc.rotate_op(180)]))
        assert out.size == (64, 48) and pixel(out, (63, 47)) == BLUE

    def test_rotate_counter_clockwise(self, img):
        out = svc.apply_edits(img, svc.ImageEdits([svc.rotate_op(-90)]))
        assert out.size == (48, 64) and pixel(out, (0, 63)) == BLUE

    def test_flips(self, img):
        horizontal = svc.apply_edits(img, svc.ImageEdits([svc.flip_op("h")]))
        vertical = svc.apply_edits(img, svc.ImageEdits([svc.flip_op("v")]))
        assert pixel(horizontal, (63, 0)) == BLUE and pixel(vertical, (0, 47)) == BLUE

    def test_crop_fractions(self, img):
        out = svc.apply_edits(img, svc.ImageEdits([svc.crop_op(0, 0, 0.25, 0.5)]))
        assert out.size == (16, 24) and pixel(out, (0, 0)) == BLUE

    def test_crop_after_rotation_uses_rotated_frame(self, img):
        edits = svc.ImageEdits([svc.rotate_op(90), svc.crop_op(0.5, 0, 1, 0.5)])
        out = svc.apply_edits(img, edits)
        assert out.size == (24, 32) and pixel(out, (23, 0)) == BLUE

    def test_empty_crop_rejected(self):
        with pytest.raises(ValueError):
            svc.crop_op(0.5, 0.5, 0.5, 0.9)

    def test_resize_modes(self, img):
        assert svc.apply_edits(img, svc.ImageEdits([svc.resize_op("percent", 50)])).size == (32, 24)
        assert svc.apply_edits(img, svc.ImageEdits([svc.resize_op("fit", 32, 32)])).size == (32, 24)
        assert svc.apply_edits(img, svc.ImageEdits([svc.resize_op("exact", 10, 10)])).size == (10, 10)

    def test_size_after_matches_real_result(self, img):
        ops = [svc.rotate_op(90), svc.crop_op(0.1, 0.1, 0.9, 0.7), svc.resize_op("percent", 150)]
        assert svc.size_after(img.size, ops) == svc.apply_edits(img, svc.ImageEdits(ops)).size

    def test_preview_scaling_of_resize(self, img):
        preview = img.resize((32, 24))  # a half-size preview of the 64x48 original
        out = svc.apply_edits(preview, svc.ImageEdits([svc.resize_op("exact", 40, 20)]), full_size=(64, 48))
        assert out.size == (20, 10)

    def test_brightness_and_contrast(self, img):
        darker = svc.apply_edits(img, svc.ImageEdits(brightness=0.5))
        assert pixel(darker, (40, 40))[0] == pytest.approx(RED[0] * 0.5, abs=2)
        flat = svc.apply_edits(img, svc.ImageEdits(contrast=0.0))
        assert len(flat.getcolors()) == 1

    def test_describe(self):
        edits = svc.ImageEdits([svc.rotate_op(90), svc.flip_op("h")], brightness=1.2)
        assert edits.describe() == "Rotate 90° right, Flip horizontally, Brightness 120%"
        assert svc.ImageEdits().is_identity


class TestLoadAndSave:
    def test_exif_orientation_is_applied(self, image_factory):
        # Orientation 6 = the camera was turned; viewers rotate 90 degrees clockwise.
        path = image_factory("phone.jpg", size=(64, 48), orientation=6)
        loaded = svc.load_image(path)
        assert loaded.image.size == (48, 64) and loaded.full_size == (48, 64)

    def test_saved_file_has_orientation_reset(self, image_factory, tmp_path):
        path = image_factory("phone.jpg", size=(64, 48), orientation=6)
        loaded = svc.load_image(path)
        out = tmp_path / "out.jpg"
        svc.save_image(loaded.image, out, exif=loaded.exif)
        with Image.open(out) as saved:
            assert saved.size == (48, 64)
            assert saved.getexif().get(0x0112, 1) == 1  # not rotated a second time by viewers

    def test_preview_load_is_small(self, image_factory):
        path = image_factory("big.jpg", size=(2000, 1000))
        loaded = svc.load_image(path, max_size=400)
        assert max(loaded.image.size) <= 400 and loaded.full_size == (2000, 1000)

    @pytest.mark.parametrize("ext", ["jpg", "png", "webp", "bmp", "tiff", "gif", "ico"])
    def test_every_output_format(self, image_factory, tmp_path, ext):
        source = image_factory("src.png", mode="RGBA", color=(10, 200, 30, 128))
        loaded = svc.load_image(source)
        out = tmp_path / f"out.{ext}"
        svc.save_image(loaded.image, out, quality=80)
        with Image.open(out) as saved:
            assert saved.format == svc.PIL_FORMATS[ext]

    def test_sixteen_bit_grayscale_is_not_white(self, tmp_path):
        path = tmp_path / "gray16.png"
        Image.new("I;16", (8, 8), 30000).save(path)
        loaded = svc.load_image(path)
        assert loaded.image.mode == "L" and 100 < loaded.image.getpixel((0, 0)) < 130

    def test_unreadable_file(self, tmp_path):
        bad = tmp_path / "fake.jpg"
        bad.write_text("not an image")
        with pytest.raises(InvalidInputError):
            svc.load_image(bad)

    def test_unsupported_output(self, tmp_path):
        with pytest.raises(InvalidInputError):
            svc.save_image(Image.new("RGB", (4, 4)), tmp_path / "x.xyz")

    def test_no_partial_files_left(self, image_factory, tmp_path):
        loaded = svc.load_image(image_factory("a.png"))
        svc.save_image(loaded.image, tmp_path / "ok.png")
        assert not [p for p in tmp_path.iterdir() if "kmt-partial" in p.name]


class TestBatch:
    def test_process_images_reports_failures(self, image_factory, tmp_path):
        good = image_factory("good.png")
        bad = tmp_path / "bad.png"
        bad.write_text("broken")
        out_dir = tmp_path / "out"
        result = svc.process_images(
            [svc.ImageTask(good, svc.ImageEdits([svc.rotate_op(90)])), svc.ImageTask(bad, svc.ImageEdits())],
            out_dir, "jpg", 85, "_edited", True, JobContext())
        assert [p.name for p in result.outputs] == ["good_edited.jpg"]
        assert result.warnings and "bad.png" in result.warnings[0]
        with Image.open(out_dir / "good_edited.jpg") as saved:
            assert saved.size == (48, 64)

    def test_all_failing_raises(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_text("broken")
        with pytest.raises(ProcessingError):
            svc.process_images([svc.ImageTask(bad, svc.ImageEdits())], tmp_path / "o", "png", 90, "", True, JobContext())

    def test_existing_outputs_are_not_overwritten_by_default(self, image_factory, tmp_path):
        good = image_factory("pic.png")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "pic.png").write_text("keep me")
        result = svc.process_images([svc.ImageTask(good, svc.ImageEdits())], out_dir, "keep", 90, "", True, JobContext())
        assert result.outputs[0].name == "pic (1).png" and (out_dir / "pic.png").read_text() == "keep me"


def test_list_images_in_folder(tmp_path, image_factory):
    for name in ("img10.png", "img2.jpg", "notes.txt", "img1.webp"):
        path = tmp_path / name
        if name.endswith(".txt"):
            path.write_text("x")
        else:
            image_factory(name)
    assert [p.name for p in svc.list_images_in_folder(tmp_path)] == ["img1.webp", "img2.jpg", "img10.png"]


def test_rename_file(image_factory, tmp_path):
    path = image_factory("old.png")
    renamed = svc.rename_file(path, "new name")
    assert renamed.name == "new name.png" and renamed.exists() and not path.exists()
    image_factory("other.png")
    with pytest.raises(InvalidInputError):
        svc.rename_file(renamed, "other")
    case_only = svc.rename_file(renamed, "New Name")
    assert case_only.name == "New Name.png" and case_only.exists()


def test_date_taken(tmp_path):
    path = tmp_path / "exif.jpg"
    img = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2020:01:02 03:04:05"
    img.save(path, exif=exif.tobytes())
    taken = svc.image_date_taken(path)
    assert taken is not None
    import datetime as dt

    assert dt.datetime.fromtimestamp(taken).year == 2020
    assert svc.image_date_taken(Path(tmp_path / "missing.jpg")) is None
