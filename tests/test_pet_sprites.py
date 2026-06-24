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
    def test_current_9row_taxonomy_mapping(self):
        # A full 9-row Petdex atlas: each state must resolve to its *canonical*
        # physical row, not the order it's requested in.
        #   0 idle | 3 waving | 4 jumping | 5 failed | 6 waiting | 7 running | 8 review
        img = make_sheet([1, 2, 3, 4, 5, 6, 7, 8, 9])  # 9 rows, distinct frame counts
        states = ["idle", "wave", "run", "failed", "review", "jump", "waiting"]
        row_by, frames_by = sprites.analyze(img, states, frame_w=FW, frame_h=FH)
        assert row_by == {
            "idle": 0,
            "wave": 3,      # "waving"
            "run": 7,       # "running" (not row 1/2 running-right/left)
            "failed": 5,
            "review": 8,
            "jump": 4,      # "jumping"
            "waiting": 6,
        }
        # frame counts come from the *resolved* row (row r has r+1 frames here)
        assert frames_by["run"] == 8       # row 7
        assert frames_by["review"] == 9    # row 8
        assert frames_by["wave"] == 4      # row 3

    def test_legacy_8row_waiting_falls_back_to_idle(self):
        img = make_sheet([1, 1, 1, 1, 1, 1, 1, 1])  # 8 rows → legacy taxonomy
        states = ["idle", "wave", "run", "failed", "review", "jump", "waiting"]
        row_by, _ = sprites.analyze(img, states, frame_w=FW, frame_h=FH)
        assert row_by["idle"] == 0
        assert row_by["wave"] == 1
        assert row_by["run"] == 2
        assert row_by["failed"] == 3
        assert row_by["review"] == 4
        assert row_by["jump"] == 5
        assert row_by["waiting"] == 0  # absent on legacy sheets → idle row

    def test_trailing_blanks_trimmed_on_resolved_row(self):
        # 9-row sheet; idle row 0 has 3 frames then blanks → trimmed to 3.
        rows = [3, 0, 0, 0, 0, 0, 0, 0, 0]
        img = make_sheet(rows)
        _, frames_by = sprites.analyze(img, ["idle"], frame_w=FW, frame_h=FH)
        assert frames_by["idle"] == 3

    def test_empty_sheet_returns_empty(self):
        img = Image.new("RGBA", (0, 0), (0, 0, 0, 0))
        row_by, frames_by = sprites.analyze(img, ["idle"], frame_w=FW, frame_h=FH)
        assert row_by == {} and frames_by == {}

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
