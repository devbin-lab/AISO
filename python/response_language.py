"""Request-scoped response-language selection for Aiso.

The model must never infer its answer language from attached files, RAG passages,
or tool results.  Those can legitimately be in a different language from the
user's request.  This module therefore operates only on the original typed user
messages, before attachment context is appended.

It intentionally identifies a *language*, not a country.  A short Latin-script
prompt cannot reliably identify a country, and treating English as a country is
incorrect.  The detector is conservative: distinctive writing systems win;
common Latin-language words are used only when there is enough signal; otherwise
English is the interoperable fallback for a Latin-script prompt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ResponseLanguage:
    """A stable language code and the English name used in model instructions."""

    code: str
    name: str


_LANGUAGES: dict[str, ResponseLanguage] = {
    "ar": ResponseLanguage("ar", "Arabic"),
    "de": ResponseLanguage("de", "German"),
    "en": ResponseLanguage("en", "English"),
    "es": ResponseLanguage("es", "Spanish"),
    "fr": ResponseLanguage("fr", "French"),
    "he": ResponseLanguage("he", "Hebrew"),
    "hi": ResponseLanguage("hi", "Hindi"),
    "it": ResponseLanguage("it", "Italian"),
    "ja": ResponseLanguage("ja", "Japanese"),
    "ko": ResponseLanguage("ko", "Korean"),
    "pt": ResponseLanguage("pt", "Portuguese"),
    "ru": ResponseLanguage("ru", "Russian"),
    "th": ResponseLanguage("th", "Thai"),
    "zh": ResponseLanguage("zh", "Chinese"),
}

_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ']+")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_LOW_SIGNAL_FOLLOW_UPS = frozenset(
    {
        "continue",
        "go on",
        "next",
        "yes",
        "no",
        "ok",
        "okay",
        "please",
        "thanks",
        "thank you",
    }
)

_OUTPUT_WORDS_KO = r"(?:답변|응답|대답|작성|출력|번역|설명)"
_EXPLICIT_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ko", re.compile(rf"(?:(?:한국어|한글)\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|{_OUTPUT_WORDS_KO}\s*(?:은|는|을|를)?\s*(?:한국어|한글)\s*(?:로|로만)?|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?(?:korean|hangul)\b|\bin\s+(?:korean|hangul)\b|\b(?:korean|hangul)\s+(?:please|only)\b)", re.IGNORECASE)),
    ("en", re.compile(rf"(?:영어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|{_OUTPUT_WORDS_KO}\s*(?:은|는|을|를)?\s*영어\s*(?:로|로만)?|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?english\b|\bin\s+english\b|\benglish\s+(?:please|only)\b)", re.IGNORECASE)),
    ("ja", re.compile(rf"(?:일본어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|{_OUTPUT_WORDS_KO}\s*(?:은|는|을|를)?\s*일본어\s*(?:로|로만)?|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?japanese\b|\bin\s+japanese\b)", re.IGNORECASE)),
    ("zh", re.compile(rf"(?:중국어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|{_OUTPUT_WORDS_KO}\s*(?:은|는|을|를)?\s*중국어\s*(?:로|로만)?|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?chinese\b|\bin\s+chinese\b)", re.IGNORECASE)),
    ("es", re.compile(rf"(?:스페인어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?spanish\b|\bin\s+spanish\b)", re.IGNORECASE)),
    ("fr", re.compile(rf"(?:프랑스어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?french\b|\bin\s+french\b)", re.IGNORECASE)),
    ("de", re.compile(rf"(?:독일어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?german\b|\bin\s+german\b)", re.IGNORECASE)),
    ("pt", re.compile(rf"(?:포르투갈어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?portuguese\b|\bin\s+portuguese\b)", re.IGNORECASE)),
    ("it", re.compile(rf"(?:이탈리아어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?italian\b|\bin\s+italian\b)", re.IGNORECASE)),
    ("ru", re.compile(rf"(?:러시아어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?russian\b|\bin\s+russian\b)", re.IGNORECASE)),
    ("ar", re.compile(rf"(?:아랍어\s*(?:로|로만)\s*{_OUTPUT_WORDS_KO}|\b(?:answer|reply|respond|write|output|translate|explain)\s+(?:in\s+)?arabic\b|\bin\s+arabic\b)", re.IGNORECASE)),
)

# A deliberately small, high-signal vocabulary is safer than attempting to
# guess a country from arbitrary Latin text.  English remains the fallback for
# Latin script when the evidence is weak.
_LATIN_HINTS: dict[str, frozenset[str]] = {
    "de": frozenset({"bitte", "danke", "und", "nicht", "für", "kannst", "zeige", "erstelle"}),
    "es": frozenset({"por", "favor", "gracias", "puedes", "muestra", "crear", "resumen", "hola"}),
    "fr": frozenset({"bonjour", "merci", "s'il", "vous", "pouvez", "créer", "résumé", "bonjour"}),
    "it": frozenset({"per", "favore", "grazie", "puoi", "mostra", "creare", "riassunto", "ciao"}),
    "pt": frozenset({"por", "favor", "obrigado", "você", "pode", "mostrar", "criar", "resumo"}),
    "en": frozenset({"please", "show", "create", "make", "summarize", "summary", "what", "the", "and", "hello"}),
}


def normalize_response_language(value: str | None) -> str:
    """Return a supported language code, with English as the safe fallback."""
    code = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in _LANGUAGES else "en"


def response_language_name(value: str | None) -> str:
    """Return the English language name safe for an English system prompt."""
    return _LANGUAGES[normalize_response_language(value)].name


def _count_in_ranges(text: str, ranges: tuple[tuple[int, int], ...]) -> int:
    return sum(any(start <= ord(char) <= end for start, end in ranges) for char in text)


def _meaningful_text(text: str) -> str:
    value = _CODE_FENCE_RE.sub(" ", str(text or ""))
    # Paths, URLs, and identifiers are weak language evidence.  Keep natural
    # words around them while removing the densest misleading token forms.
    value = re.sub(r"https?://\S+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s]+", " ", value)
    return value.strip()


def detect_response_language(text: str) -> str | None:
    """Detect a substantive prompt's language, or ``None`` when it is unclear."""
    value = _meaningful_text(text)
    if not value:
        return None

    for code, pattern in _EXPLICIT_LANGUAGE_PATTERNS:
        if pattern.search(value):
            return code

    # Distinctive scripts take priority over borrowed Latin model names, file
    # paths, and code fragments that may appear in the same request.
    if _count_in_ranges(value, ((0xAC00, 0xD7AF), (0x1100, 0x11FF))) > 0:
        return "ko"
    if _count_in_ranges(value, ((0x3040, 0x30FF), (0x31F0, 0x31FF))) > 0:
        return "ja"
    if _count_in_ranges(value, ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))) > 0:
        return "zh"
    if _count_in_ranges(value, ((0x0400, 0x052F),)) > 0:
        return "ru"
    if _count_in_ranges(value, ((0x0600, 0x06FF), (0x0750, 0x077F))) > 0:
        return "ar"
    if _count_in_ranges(value, ((0x0590, 0x05FF),)) > 0:
        return "he"
    if _count_in_ranges(value, ((0x0900, 0x097F),)) > 0:
        return "hi"
    if _count_in_ranges(value, ((0x0E00, 0x0E7F),)) > 0:
        return "th"

    words = [word.casefold() for word in _WORD_RE.findall(value)]
    if not words:
        return None
    normalized_follow_up = re.sub(r"[^a-z ]", "", value.casefold()).strip()
    if normalized_follow_up in _LOW_SIGNAL_FOLLOW_UPS:
        # Preserve the previous substantive request's language.  A Korean
        # conversation followed by "continue" must not unexpectedly switch to
        # English merely because the follow-up is Latin script.
        return None
    scores = {
        code: sum(word in hints for word in words)
        for code, hints in _LATIN_HINTS.items()
    }
    best = max(scores, key=scores.get)
    best_score = scores[best]
    if best_score >= 2:
        return best
    if best_score == 1 and len(words) <= 4:
        return best
    # A meaningful Latin-script prompt with no strong alternate signal is most
    # safely handled as English.  The original prompt remains in context, so
    # the model can still recognize a language that this compact detector did
    # not name explicitly.
    return "en"


def response_language_from_messages(
    messages: Iterable[Mapping[str, object]], *, fallback: str = "ko"
) -> str:
    """Pick the latest substantive original user-message language.

    A short follow-up such as ``continue`` has weak language evidence.  Walking
    backward through original user messages deliberately preserves the language
    of the earlier substantive request instead of switching because of an
    attachment, tool result, or assistant response.
    """
    fallback_code = normalize_response_language(fallback)
    for message in reversed(list(messages)):
        if str(message.get("role") or "") != "user":
            continue
        detected = detect_response_language(str(message.get("content") or ""))
        if detected is not None:
            return detected
    return fallback_code


__all__ = [
    "ResponseLanguage",
    "detect_response_language",
    "normalize_response_language",
    "response_language_from_messages",
    "response_language_name",
]
