"""Compare dotted version strings. Pure, so it is unit-tested without Anki."""


def parse(v):
    out = []
    for part in str(v or "").strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def newer(candidate, current):
    """True when `candidate` is a strictly higher version than `current`."""
    if not str(candidate or "").strip():
        return False
    a, b = parse(candidate), parse(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))
