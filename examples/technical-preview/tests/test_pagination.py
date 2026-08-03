from __future__ import annotations

import unittest

from audit_api import page


class PaginationTests(unittest.TestCase):
    def test_returns_requested_window(self) -> None:
        self.assertEqual(page([1, 2, 3, 4], offset=1, limit=2), [2, 3])

    def test_rejects_invalid_bounds(self) -> None:
        with self.assertRaises(ValueError):
            page([1], offset=-1)
        with self.assertRaises(ValueError):
            page([1], limit=0)


if __name__ == "__main__":
    unittest.main()
