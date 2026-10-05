"""The language the owner actually speaks to an agent, owned by the host.

A new voice conversation used to start in the client's interface language,
so an owner who speaks Turkish with an English interface was greeted in
English. The host now learns the spoken language from the owner's own
transcribed speech (``voice.append``, every client sends it) and from the
model's language reports (``voice.language``), keeps it per owner and agent,
and opens a new conversation in it.

Detection is deliberately conservative: a short or ambiguous utterance says
nothing, and transcribed speech moves the preference only when the same new
language is heard twice in a row (one stray English sentence does not flip a
Turkish speaker). A model report is authoritative and applies at once.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

LANGUAGES = ("en", "tr", "es")
MAX_OWNERS = 1_000

_STOPWORDS = {
    "tr": frozenset("""ve bir bu da de ne mi mı mu mü için ben sen biz siz o çok ama şey var yok nasıl neden niye
        evet hayır tamam gibi daha ile ki şu şimdi bana sana beni seni bunu onu her hiç olarak kadar sonra önce
        değil ya yani bak hadi lütfen merhaba selam teşekkürler sağol peki iyi""".split()),
    "en": frozenset("""the and is are you what how i it to of a in that do can please yes no this was be have
        with for on my your me we they not but so just like about would could should there here hello thanks
        okay what's i'm it's don't""".split()),
    "es": frozenset("""el la los las que de y es por para una un como qué cómo sí no con en lo se me te mi tu
        pero muy más este esta eso hola gracias bien está estoy tengo quiero puedes""".split()),
}
_MARKS = {
    "tr": re.compile(r"[ğışĞİŞ]"),
    "es": re.compile(r"[ñ¿¡áéíóúÑÁÉÍÓÚ]"),
}
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def detect_language(text: str) -> str | None:
    """en, tr or es when the utterance clearly is one of them, else None."""
    words = [word.lower() for word in _WORD.findall(text or "")]
    if len(words) < 3:
        return None
    scores = {language: sum(word in stopwords for word in words) for language, stopwords in _STOPWORDS.items()}
    for language, marks in _MARKS.items():
        scores[language] += 2 * len(marks.findall(text))
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    # Clear evidence only: enough hits and well ahead of the runner-up.
    if top < 2 or top < 2 * second + 1:
        return None
    return best


class VoiceLanguagePreferences:
    """Per owner and agent: {"language", "candidate", "candidateCount", "updatedAt"}.

    One small JSON file in the host's state directory, written atomically
    under a lock; bounded to the most recent owners.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def key(owner: dict, bot_id: str) -> str:
        who = owner.get("uid") if owner.get("kind") == "account" else "host"
        return f"{who}:{bot_id}"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        if len(data) > MAX_OWNERS:
            newest = sorted(data.items(), key=lambda item: str(item[1].get("updatedAt", "")), reverse=True)
            data = dict(newest[:MAX_OWNERS])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".voice_language.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get(self, owner: dict, bot_id: str) -> str | None:
        with self._lock:
            entry = self._read().get(self.key(owner, bot_id))
        language = entry.get("language") if isinstance(entry, dict) else None
        return language if language in LANGUAGES else None

    def heard(self, owner: dict, bot_id: str, language: str) -> None:
        """Transcribed speech: a new language must be heard twice in a row."""
        self._update(owner, bot_id, language, authoritative=False)

    def reported(self, owner: dict, bot_id: str, language: str) -> None:
        """The model's own language report: applies at once."""
        self._update(owner, bot_id, language, authoritative=True)

    def _update(self, owner: dict, bot_id: str, language: str, *, authoritative: bool) -> None:
        if language not in LANGUAGES:
            return
        with self._lock:
            data = self._read()
            key = self.key(owner, bot_id)
            entry = data.get(key) if isinstance(data.get(key), dict) else {}
            current = entry.get("language")
            if language == current:
                entry.pop("candidate", None)
                entry.pop("candidateCount", None)
            elif authoritative or current not in LANGUAGES:
                entry = {"language": language}
            elif entry.get("candidate") == language and entry.get("candidateCount", 0) >= 1:
                entry = {"language": language}
            else:
                entry["candidate"] = language
                entry["candidateCount"] = 1
            entry["updatedAt"] = datetime.now(timezone.utc).isoformat()
            data[key] = entry
            self._write(data)
