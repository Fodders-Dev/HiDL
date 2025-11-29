import time
from typing import List, Dict, Tuple

_PAGES: Dict[Tuple[int, str], List[str]] = {}


def split_text(text: str, max_len: int = 900) -> List[str]:
    """Split text into chunks close to max_len without breaking words."""
    parts: List[str] = []
    buf = []
    count = 0
    for line in text.split("\n"):
        if count + len(line) + 1 > max_len:
            parts.append("\n".join(buf))
            buf = [line]
            count = len(line) + 1
        else:
            buf.append(line)
            count += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts


def start_paged(text: str, chat_id: int) -> Tuple[str, List[str]]:
    """Store chunks for chat and return key + first chunk list."""
    chunks = split_text(text)
    key = str(int(time.time() * 1000))
    _PAGES[(chat_id, key)] = chunks
    return key, chunks


def get_page(chat_id: int, key: str, index: int) -> str:
    chunks = _PAGES.get((chat_id, key), [])
    if 0 <= index < len(chunks):
        if index == len(chunks) - 1:
            # cleanup when last page sent
            _PAGES.pop((chat_id, key), None)
        return chunks[index]
    return ""
