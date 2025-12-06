"""Unit tests for helper utilities."""

import unittest

from youtube_download480p.app import select_video_format, slugify


def build_format(format_id: str, height: int, tbr: int) -> dict:
    return {
        "format_id": format_id,
        "height": height,
        "tbr": tbr,
        "vcodec": "h264",
        "acodec": "none",
    }


class SelectionTests(unittest.TestCase):
    def test_selects_exact_480_format(self) -> None:
        formats = [
            build_format("137", 1080, 5000),
            build_format("135", 480, 2500),
        ]
        info = {"formats": formats}
        fmt, height = select_video_format(info)
        self.assertEqual(fmt, "135")
        self.assertEqual(height, 480)

    def test_selects_smallest_above_480_when_needed(self) -> None:
        formats = [
            build_format("137", 1080, 5000),
            build_format("136", 720, 3500),
            build_format("134", 360, 1000),
        ]
        info = {"formats": formats}
        fmt, height = select_video_format(info)
        self.assertEqual(fmt, "136")
        self.assertEqual(height, 720)

    def test_fallbacks_to_best_when_no_video_above_480(self) -> None:
        formats = [
            build_format("134", 360, 1000),
            build_format("133", 240, 500),
        ]
        info = {"formats": formats}
        fmt, height = select_video_format(info)
        self.assertEqual(fmt, "134")
        self.assertEqual(height, 360)

    def test_slugify_handles_problematic_titles(self) -> None:
        self.assertEqual(slugify("Video:Title?"), "Video_Title")
        self.assertEqual(slugify("   "), "video")


if __name__ == "__main__":
    unittest.main()
