# Tiny Spec Under Review

This is a minimal artifact used by the e2e test to drive a real `codex exec`
through `bulldozer:check`. It contains one obvious correctness gap so codex has
something to find — without that, the parser path is untested end-to-end.

## API

```python
def divide(a: float, b: float) -> float:
    return a / b
```

The function divides `a` by `b` and returns the result.

## Guarantees

- Returns a float
- No error handling needed — the caller is responsible

(Yes, the lack of a zero-division check is the intentional finding for the e2e test.)
