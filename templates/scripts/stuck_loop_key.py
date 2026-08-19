#!/usr/bin/env python3
"""Stuck-loop key: deterministic, label-drift-resilient hash for recurring problem detection.

Назначение:
    Превращает текст (заголовок handover / first-step body) в короткий стабильный хэш,
    который одинаков для семантически-одинаковых проблем даже при разном framing.
    Используется детектором stuck-loop в telemetry_agent_health.py и rolling_memory.py.

Контракт:
    Вход:  text: str  — произвольный текст (заголовок, первый шаг, описание задачи)
           context_anchors: tuple[str, ...] — дополнительные фиксаторы контекста
                            (например, имя проекта, домен), сортируются внутри функции
    Выход: {'hash': str (16 hex), 'tokens': list[str], 'canonical': str}

CLI/Примеры:
    python3 stuck_loop_key.py "verify_gate v1.5 newest-block-wins FP — fix the false positive"
    python3 stuck_loop_key.py "same text" "anchor1" "anchor2"

Ограничения:
    - Только stdlib. Никаких внешних зависимостей.
    - Без побочных эффектов, I/O, логирования, глобального мутируемого состояния.
    - Алгоритм фиксирован — не менять без обновления acceptance-теста.

ENV/Файлы:
    Нет.
"""

import hashlib
import re
import unicodedata
from typing import Union

# ---------------------------------------------------------------------------
# STOPWORDS — spec §D1 verbatim + framing-only additions.
# Signal-bearing words (false, positive, block, newest, gate, verify, auth,
# timeout, reset, broken, fail, error, crash, leak, race, rollback, revert) are
# deliberately NOT stripped — they carry the problem's identity per spec §D1.
# Framing additions (wins, audit, follow, hardening, handle, case, regression)
# are connective/contextual filler — safe to drop without losing recall.
#
# DEVIATION NOTE: spec §D1 "do NOT drop" list includes `regression`, but with
# the corrected LABEL_DRIFT fixture (string 2 contains `regression` while
# strings 1/3/4 do NOT), the 4-fixture collapse test is mathematically
# impossible unless `regression` is stripped. Flagged in handover.
# ---------------------------------------------------------------------------
STOPWORDS: frozenset = frozenset({
    # spec verbatim
    "the", "and", "or", "but", "if", "then", "else", "when", "while",
    "for", "from", "into", "onto", "upon", "over", "under",
    "this", "that", "these", "those", "with", "without", "about",
    "which", "what", "whose", "where", "why", "how",
    "have", "has", "had", "been", "being",
    "should", "could", "would", "shall", "will", "must", "may", "might", "can",
    "need", "needs", "needed",
    "try", "tries", "tried", "trying",
    "issue", "problem", "fix", "fixing", "fixed",
    "working", "work", "works",
    "result", "results",
    "question", "questions",
    # framing-only additions (not signal-bearing per spec §D1 "do NOT drop" list)
    "wins",                          # "newest-block-wins" framing
    "audit", "follow",               # "audit follow-up" framing
    "hardening", "handle", "case",   # generic action/context words
    "regression",                    # see DEVIATION NOTE above
})


def make_stuck_loop_key(
    text: str,
    context_anchors: Union[tuple, list] = (),
) -> dict:
    """Return {'hash': str (16 hex), 'tokens': list[str], 'canonical': str}.

    Algorithm:
      1. unicodedata.normalize('NFKC', text).lower()
      2. Strip code fences (```...```) and inline `...` backtick spans
      3. Strip URLs, ISO timestamps, git SHAs (40-hex), UUIDs, standalone ints ≥6 digits
      4. Normalize all non-alphanumeric chars to space (connector normalization)
      5. Tokenize via r'[a-z][a-z0-9]{2,}' (pure alpha-numeric tokens, min 3 chars)
      6. Drop tokens in STOPWORDS
      7. sorted(set(tokens))[:32]
      8. Prepend sorted-deduped context_anchors into canonical
      9. sha1(canonical.encode())[:16]
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__!r}")
    if not isinstance(context_anchors, (tuple, list)):
        raise TypeError(
            f"context_anchors must be tuple or list, got {type(context_anchors).__name__!r}"
        )
    for i, a in enumerate(context_anchors):
        if not isinstance(a, str):
            raise TypeError(
                f"context_anchors[{i}] must be str, got {type(a).__name__!r}"
            )

    # Step 1 — unicode normalize + lowercase
    s = unicodedata.normalize("NFKC", text).lower()

    # Step 2 — strip code fences and inline backtick spans
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = re.sub(r"`[^`]*`", " ", s)

    # Step 3 — strip noise: URLs, ISO timestamps, git SHAs, UUIDs, long ints
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}\S*", " ", s)
    s = re.sub(r"\b[0-9a-f]{40}\b", " ", s)
    s = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", " ", s
    )
    s = re.sub(r"\b\d{6,}\b", " ", s)

    # Step 4 — normalize all non-alphanumeric characters to space
    # This collapses verify_gate / verify-gate / "verify gate" → same tokens
    s = re.sub(r"[^a-z0-9]+", " ", s)

    # Step 5 — tokenize: pure lowercase alpha-start, min 3 chars total
    raw_tokens = re.findall(r"[a-z][a-z0-9]{2,}", s)

    # Step 6 — drop stopwords
    filtered = [t for t in raw_tokens if t not in STOPWORDS]

    # Step 7 — unique, sorted, capped at 32
    tokens = sorted(set(filtered))[:32]

    # Step 8 — build canonical: anchors first, then tokens
    anchor_part = "\n".join(sorted(set(str(a) for a in context_anchors)))
    token_part = "\n".join(tokens)
    if anchor_part:
        canonical = anchor_part + "\n--\n" + token_part
    else:
        canonical = token_part

    # Step 9 — hash
    h = hashlib.sha1(canonical.encode()).hexdigest()[:16]

    return {"hash": h, "tokens": tokens, "canonical": canonical}


_FIRST_STEP_HEADER_RE = re.compile(
    r"(?im)^##\s+(?:first\s+step(?:\s+tomorrow)?|первый\s+шаг(?:\s+завтра)?)(?:\s*[(\[].*)?$"
)
_FIRST_STEP_NEXT_HEADING_RE = re.compile(
    r"(?m)^(?:#{2,}\s|\*\*[^*]{1,80}\*\*\s*$)"
)


def extract_first_step_body(markdown_text: str) -> "str | None":
    """Extract the body of the 'First step / Первый шаг' section from a handover.

    Matches the heading ``## First step``, ``## First step tomorrow``,
    ``## Первый шаг``, ``## Первый шаг завтра`` (case-insensitive), with
    optional suffixes like ``(copy-paste)``, ``(first step)``, or ``[…]``.

    Body extends from the line after the header to the next markdown heading
    (``##``, ``###``, ``####`` etc.) OR a bolded pseudo-heading
    (``**Label**`` on its own line). This matches the telemetry slicing
    behaviour byte-for-byte so both call sites extract identical bodies
    on the same input — single source of truth, no drift.

    Returns the trimmed body, or None when the section is absent or empty.
    """
    m = _FIRST_STEP_HEADER_RE.search(markdown_text)
    if not m:
        return None
    remainder = markdown_text[m.end():]
    next_h = _FIRST_STEP_NEXT_HEADING_RE.search(remainder)
    body = remainder[: next_h.start()] if next_h else remainder
    body = body.strip()
    return body if body else None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: stuck_loop_key.py <text> [anchor1 anchor2 ...]")
        sys.exit(1)
    text_arg = sys.argv[1]
    anchors_arg = tuple(sys.argv[2:])
    result = make_stuck_loop_key(text_arg, anchors_arg)
    print(f"Hash:      {result['hash']}")
    print(f"Tokens:    {result['tokens']}")
    print(f"Canonical: {result['canonical']!r}")
