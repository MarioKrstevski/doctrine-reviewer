"""Split a sequence into batches.

Registration sends an entire deck -- every note's fields, rendered HTML
and note type CSS. As one request that exceeded the HTTP timeout on a
real deck and the upload was aborted mid-flight, committing nothing.
Batching bounds each request instead of scaling with deck size.
"""


def chunked(items, size):
    size = max(1, int(size or 1))
    for start in range(0, len(items), size):
        yield items[start:start + size]
