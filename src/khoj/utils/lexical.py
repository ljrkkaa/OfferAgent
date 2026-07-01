import re
from typing import Any

TERM_RE = re.compile(r"[A-Za-z0-9_+#.-]+|[\u4e00-\u9fff]+")


def message_text(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("message") or message.get("content") or "")
    return str(getattr(message, "message", "") or getattr(message, "content", ""))


def query_terms(
    query: str,
    *,
    max_terms: int = 16,
    cjk_sizes: tuple[int, ...] = (),
    ignore_prefixes: tuple[str, ...] = (),
) -> list[str]:
    candidates: list[str] = []
    for token in TERM_RE.findall(query or ""):
        term = token.strip("+#.-")
        key = term.lower()
        if len(term) < 2 or key.startswith(ignore_prefixes):
            continue
        candidates.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            for size in cjk_sizes:
                if size >= len(term):
                    continue
                candidates.extend(term[start : start + size] for start in range(len(term) - size + 1))

    return list(dict.fromkeys(term for term in candidates if len(term) >= 2))[:max_terms]
