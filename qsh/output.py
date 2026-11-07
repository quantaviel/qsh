"""Text reply limits shared by builtins and external programs."""

OUTPUT_LIMIT = 1500
TRUNCATED = "\n…（输出已截断）"


def bounded(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text or "（空）"
    return text[: OUTPUT_LIMIT - len(TRUNCATED)] + TRUNCATED
