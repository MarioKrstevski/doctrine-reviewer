"""Registration uploads a whole deck, so it must be sent in batches.

A single request carrying every note's fields, rendered HTML and CSS
exceeded the add-on's HTTP timeout on a real deck: the client aborted at
15s (the server logged a 499) and nothing was committed.
"""

import unittest

from chunk_util import chunked


class ChunkedTest(unittest.TestCase):
    def test_an_empty_list_yields_nothing(self):
        self.assertEqual([], list(chunked([], 10)))

    def test_a_short_list_is_one_batch(self):
        self.assertEqual([[1, 2, 3]], list(chunked([1, 2, 3], 10)))

    def test_an_exact_multiple_does_not_emit_an_empty_tail(self):
        self.assertEqual([[1, 2], [3, 4]], list(chunked([1, 2, 3, 4], 2)))

    def test_a_remainder_becomes_a_final_short_batch(self):
        self.assertEqual([[1, 2], [3]], list(chunked([1, 2, 3], 2)))

    def test_every_item_appears_exactly_once_and_in_order(self):
        items = list(range(1000))
        flat = [x for batch in chunked(items, 7) for x in batch]
        self.assertEqual(items, flat)

    def test_batches_never_exceed_the_size(self):
        for batch in chunked(list(range(1000)), 7):
            self.assertLessEqual(len(batch), 7)
            self.assertGreater(len(batch), 0)

    def test_a_nonsense_size_still_produces_valid_batches(self):
        for size in (0, -1):
            with self.subTest(size=size):
                batches = list(chunked([1, 2, 3], size))
                flat = [x for b in batches for x in b]
                self.assertEqual([1, 2, 3], flat)
