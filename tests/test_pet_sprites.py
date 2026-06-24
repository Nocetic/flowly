"""Tests for flowly.pet.sprites — row→state mapping + blank-frame trim."""

from PIL import Image

from flowly.pet import sprites

FW = FH = 4  # tiny frames keep the synthetic sheets small


def make_sheet(rows_frames: list[int], fw: int = FW, fh: int = FH) -> Image.Image:
    """Build an RGBA sheet where row *i* has ``rows_frames[i]`` opaque frames
    (from the left), the rest of the row left transparent."""
    cols = max(rows_frames) if rows_frames else 0
    img = Image.new("RGBA", (fw * cols, fh * len(rows_frames)), (0, 0, 0, 0))
    block = Image.new("RGBA", (fw, fh), (255, 0, 0, 255))
    for r, n in enumerate(rows_frames):
        for c in range(n):
            img.paste(block, (c * fw, r * fh))
    return img


class TestAnalyze:
    def test_maps_rows_and_trims_trailing_blanks(self):
        img = make_sheet([3, 1, 2])  # cols=3
        row_by, frames_by = sprites.analyze(img, ["idle", "wave", "run"], frame_w=FW, frame_h=FH)
        assert row_by == {"idle": 0, "wave": 1, "run": 2}
        assert frames_by == {"idle": 3, "wave": 1, "run": 2}

    def test_states_beyond_rows_skipped(self):
        img = make_sheet([2, 2])
        row_by, _ = sprites.analyze(img, ["a", "b", "c"], frame_w=FW, frame_h=FH)
        assert set(row_by) == {"a", "b"}

    def test_blank_row_maps_to_zero(self):
        img = make_sheet([0, 1])
        _, frames_by = sprites.analyze(img, ["x", "y"], frame_w=FW, frame_h=FH)
        assert frames_by["x"] == 0
        assert frames_by["y"] == 1

    def test_middle_blank_counts_in_run(self):
        # opaque at col 0 and col 2, blank at col 1 → 3 frames (middle kept).
        img = Image.new("RGBA", (FW * 3, FH), (0, 0, 0, 0))
        block = Image.new("RGBA", (FW, FH), (0, 0, 255, 255))
        img.paste(block, (0, 0))
        img.paste(block, (2 * FW, 0))
        assert sprites.count_frames_in_row(img, 0, frame_w=FW, frame_h=FH) == 3


class TestLoadImage:
    def test_load_image_returns_rgba(self, tmp_path):
        path = tmp_path / "sheet.png"
        make_sheet([1]).save(path)
        loaded = sprites.load_image(path)
        assert loaded.mode == "RGBA"
        assert (loaded.width, loaded.height) == (FW, FH)
