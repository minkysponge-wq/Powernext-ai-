from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from .extraction.rendering import _cached_review_page, render_review_page


class ReviewPageCacheTests(SimpleTestCase):
    def test_adjacent_crops_reuse_page_and_file_change_invalidates(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"first")
            _cached_review_page.cache_clear()
            with patch("lab.extraction.rendering.render_page", return_value=b"preview") as render:
                self.assertEqual(render_review_page(source, 1), b"preview")
                self.assertEqual(render_review_page(source, 1), b"preview")
                self.assertEqual(render.call_count, 1)
                source.write_bytes(b"second edition")
                self.assertEqual(render_review_page(source, 1), b"preview")
                self.assertEqual(render.call_count, 2)
            _cached_review_page.cache_clear()
