"""Tests for the audio downloader helpers."""

import unittest

from youtube_audio_mp3 import select_audio_format, slugify


def make_format(format_id: str, abr: int, tbr: int = 0) -> dict:
    return {
        "format_id": format_id,
        "acodec": "opus",
        "vcodec": "none",
        "abr": abr,
        "tbr": tbr,
    }


class AudioSelectionTests(unittest.TestCase):
    def test_selects_highest_bitrate_audio(self) -> None:
        formats = [
            make_format("251", 160),
            make_format("250", 70),
            make_format("140", 128),
        ]
        fmt, abr = select_audio_format({"formats": formats})
        self.assertEqual(fmt, "251")
        self.assertEqual(abr, 160)

    def test_slugify_defaults_and_sanitizes(self) -> None:
        self.assertEqual(slugify("My Song? (Live)"), "My_Song_Live")
        self.assertEqual(slugify(""), "audio")


if __name__ == "__main__":
    unittest.main()
