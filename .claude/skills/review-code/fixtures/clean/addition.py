"""Sample clean: simple addition. /review-code MUST return Approve."""
def add(a: int, b: int) -> int:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("a and b must be ints")
    return a + b
