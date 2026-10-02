# Python `urlsplit` normalization at exact-validation boundaries

## Why this matters

`urllib.parse.urlsplit()` is a structural parser, not an exact-string validator. Python may remove characters before exposing components. A validator that only compares `scheme`, `netloc`, `path`, `query`, and `fragment` can therefore accept an original string that the protocol explicitly forbids.

## Minimal reproduction

```python
from urllib.parse import urlsplit

canonical = "https://forge.example/issues/198#issuecomment-2689"
for supplied in [" " + canonical, "\t" + canonical, canonical + "\r"]:
    parsed = urlsplit(supplied)
    assert parsed.scheme == "https"
    assert parsed.netloc == "forge.example"
    assert parsed.path == "/issues/198"
    assert parsed.query == ""
    assert parsed.fragment == "issuecomment-2689"
    assert supplied != canonical
```

The parsed components look canonical while the supplied proof is not.

## Robust pattern

Use parser checks for structural safety, then compare the untouched input to a canonical string assembled only from trusted values:

```python
parsed = urlsplit(supplied)
if parsed.username is not None or parsed.password is not None:
    raise ValueError("userinfo forbidden")
if parsed.query:
    raise ValueError("query forbidden")

expected = (
    f"{trusted_origin}/{trusted_segment}/{trusted_number}"
    f"#issuecomment-{trusted_comment_id}"
)
if supplied != expected:
    raise ValueError("URL is not exact canonical form")
```

Do not rebuild `expected` from attacker-controlled parsed components. Keep component checks as defense in depth and to produce useful errors.

## Negative-test matrix

- Leading ASCII space and C0 controls.
- Trailing CR, LF, and TAB.
- Scheme case variants when the protocol requires one serialized form.
- Userinfo (`user@host`, `user:pass@host`).
- Explicit default port if canonical output omits it.
- Host case variants if canonical serialization fixes host case.
- Path prefix/suffix and duplicate slash.
- Query string, including an empty `?` if exact serialization forbids it.
- Fragment prefix/suffix and extra `#` content.
- Percent-encoded characters that decode to expected delimiters or path bytes.

## Review classification

If exact canonical evidence is a stated security or workflow requirement, acceptance of normalized prefixes/suffixes is an Important defect even when requests are not fetched from the proof URL. The proof no longer demonstrates that the persisted value is the one canonical identifier required by the protocol.
