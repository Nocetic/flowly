"""Text chunker — splits markdown files into token-bounded chunks with overlap."""

from dataclasses import dataclass


@dataclass
class Chunk:
    text: str
    start_line: int  # 1-based
    end_line: int    # 1-based inclusive


def _count_tokens(text: str) -> int:
    """Estimate token count using tiktoken (cl100k_base)."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Fallback: ~4 chars per token
        return len(text) // 4


def chunk_text(
    text: str,
    chunk_tokens: int = 400,
    overlap_tokens: int = 80,
) -> list[Chunk]:
    """
    Split text into chunks of ~chunk_tokens with overlap_tokens overlap.

    Splits on newlines, grouping lines until the token budget is reached.
    Then backtracks overlap_tokens worth of lines for the next chunk.

    Args:
        text: Full file text.
        chunk_tokens: Target max tokens per chunk.
        overlap_tokens: Tokens to repeat at the start of next chunk.

    Returns:
        List of Chunk objects with line numbers.
    """
    lines = text.splitlines()
    if not lines:
        return []

    chunks: list[Chunk] = []
    i = 0  # current line index (0-based)

    while i < len(lines):
        chunk_lines: list[str] = []
        token_count = 0
        j = i

        while j < len(lines):
            line = lines[j]
            line_tokens = _count_tokens(line + "\n")
            if chunk_lines and token_count + line_tokens > chunk_tokens:
                break
            chunk_lines.append(line)
            token_count += line_tokens
            j += 1

        if not chunk_lines:
            # Single line exceeds budget — include it anyway
            chunk_lines = [lines[i]]
            j = i + 1

        chunk_text_str = "\n".join(chunk_lines).strip()
        if chunk_text_str:
            chunks.append(Chunk(
                text=chunk_text_str,
                start_line=i + 1,
                end_line=j,
            ))

        if j >= len(lines):
            break

        # Backtrack for overlap
        overlap_so_far = 0
        new_i = j
        while new_i > i + 1:
            prev_line = lines[new_i - 1]
            overlap_so_far += _count_tokens(prev_line + "\n")
            if overlap_so_far >= overlap_tokens:
                break
            new_i -= 1

        i = max(new_i, i + 1)  # always advance at least one line

    return chunks
