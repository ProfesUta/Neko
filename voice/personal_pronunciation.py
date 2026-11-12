"""Optional private name profiles for the existing Kokoro lexicon frontend.

Only the resident worker rewrites complete spoken names to private lexicon keys.
Display/chat text and the source voice/champion dictionaries are never changed.
"""
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


_PREFIX = "capripersonalpronunciation"
_WRAPPERS = r'''["'\u201c\u201d\u2018\u2019()\[\]{}]*'''
_PUNCTUATION = set('.,;:!?()\"—…')


def _key(text):
    return " ".join(unicodedata.normalize("NFC", text).replace("\u2019", "'").casefold().split())


def read_profile(path):
    """Errors intentionally omit names, phone strings and JSON contents."""
    try:
        path = Path(path)
        if path.stat().st_size > 65536:
            raise ValueError()
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload["entries"]
        if not isinstance(entries, list) or len(entries) > 128:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("Personal pronunciation profile must contain an entries list (maximum 128 entries, 64 KiB)") from None
    result, seen = [], set()
    for index, item in enumerate(entries, 1):
        error = f"Personal pronunciation entry {index}: "
        if not isinstance(item, dict) or type(item.get("enabled", True)) is not bool:
            raise ValueError(error + "invalid entry or enabled flag")
        if not item.get("enabled", True):
            continue
        display = item.get("display_name")
        spoken = item.get("spoken_pronunciation", "")
        phones = item.get("phonemes")
        if (not isinstance(display, str) or not 1 <= len(display.strip()) <= 100
                or not re.fullmatch(r"[\w'\u2019 -]+", display.strip()) or _PREFIX in display.casefold()):
            raise ValueError(error + "display_name must be a complete name without markup/control characters")
        display = " ".join(unicodedata.normalize("NFC", display).split())
        if _key(display) in seen:
            raise ValueError(error + "duplicate display_name")
        seen.add(_key(display))
        if not isinstance(spoken, str) or len(spoken) > 300:
            raise ValueError(error + "invalid spoken_pronunciation")
        if isinstance(phones, str):
            phones = phones.split()
        if phones is not None and (not isinstance(phones, list) or not 1 <= len(phones) <= 256
                                   or any(not isinstance(phone, str) or not phone or re.search(r"\s", phone) for phone in phones)):
            raise ValueError(error + "phonemes must be whitespace-separated model token strings")
        if phones is None and not spoken.strip():
            raise ValueError(error + "provide spoken_pronunciation or phonemes, or disable this entry")
        result.append({"display_name": display, "spoken_pronunciation": spoken,
                       "phonemes": tuple(phones) if phones is not None else None})
    return tuple(result)


@dataclass(frozen=True)
class PersonalPronunciation:
    replacements: tuple
    lexicon: str

    def apply(self, text):
        for pattern, alias in self.replacements:
            text = pattern.sub(lambda match: alias + ("'s" if match.group("possessive") else ""), text)
        return text


def compile_profile(path, asset_dir, champion_lexicon):
    """Compile once per worker load. Spoken spellings must resolve locally.

    This runtime has no G2P fallback. We compose existing dictionary phones;
    unknown syllable spellings fail instead of becoming another skipped name.
    Explicit model-token phonemes take precedence over spoken spelling.
    """
    entries = read_profile(path)
    if not entries:
        return PersonalPronunciation((), "")
    asset_dir = Path(asset_dir)
    try:
        tokens = {line.rsplit(" ", 1)[0] for line in (asset_dir / "tokens.txt").read_text(encoding="utf-8").splitlines()}
        protected = {line.split()[0] for line in Path(champion_lexicon).read_text(encoding="utf-8").splitlines() if line.strip()}
        needed = set()
        for index, entry in enumerate(entries, 1):
            if entry["phonemes"] is None:
                spoken = _key(entry["spoken_pronunciation"])
                if not re.fullmatch(r"[\w' -]+", spoken):
                    raise ValueError(f"Personal pronunciation entry {index}: use dictionary words or explicit phoneme tokens")
                needed.update(re.split(r"[\s-]+", spoken))
        known = {}
        if needed:
            for dictionary in (asset_dir / "lexicon-us-en.txt", Path(champion_lexicon)):
                with dictionary.open(encoding="utf-8") as source:
                    for line in source:
                        key, _, value = line.strip().partition(" ")
                        if key in needed:
                            known[key] = tuple(value.split())
    except OSError:
        raise ValueError("Personal pronunciation could not read the installed voice dictionaries") from None
    replacements, lines = [], []
    for index, entry in enumerate(entries):
        error = f"Personal pronunciation entry {index + 1}: "
        if _key(entry["display_name"]) in protected:
            raise ValueError(error + "display_name conflicts with a protected champion name")
        phones = entry["phonemes"]
        if phones is None:
            resolved = []
            for number, word in enumerate(re.split(r"[\s-]+", _key(entry["spoken_pronunciation"])), 1):
                if word not in known:
                    raise ValueError(error + f"spoken word {number} is absent from the installed dictionaries; use explicit phoneme tokens")
                resolved.extend(known[word])
            phones = tuple(resolved)
        if not phones or any(phone not in tokens or phone in _PUNCTUATION for phone in phones):
            raise ValueError(error + "phonemes must match individual symbols in this voice pack's tokens.txt")
        alias = _PREFIX + chr(97 + index // 26) + chr(97 + index % 26)
        # Keep existing champion keys intact, including when the profile is edited.
        lines.append(alias + " " + " ".join(phones))
        last = phones[-1]
        suffix = ("ɪ", "z") if last in ("s", "z", "ʃ", "ʒ", "ʧ", "ʤ", "tʃ", "dʒ") else ("s",) if last in ("p", "t", "k", "f", "θ") else ("z",)
        if any(phone not in tokens for phone in suffix):
            raise ValueError(error + "voice pack lacks possessive suffix tokens")
        lines.append(alias + "'s " + " ".join((*phones, *suffix)))
        name = r"\s+".join(re.escape(part).replace("'", "['\u2019]") for part in entry["display_name"].replace("\u2019", "'").split())
        pattern = re.compile(r"(?<![\w'\u2019])" + _WRAPPERS + "(?:" + name + r")(?P<possessive>['\u2019]s)?" + _WRAPPERS + r"(?![\w'\u2019])", re.I)
        replacements.append((len(entry["display_name"]), pattern, alias))
    replacements.sort(key=lambda value: value[0], reverse=True)
    return PersonalPronunciation(tuple((pattern, alias) for _, pattern, alias in replacements), "\n".join(lines) + "\n")


class PersonalTts:
    """Transparent adapter around the same native engine, callbacks and voice."""
    def __init__(self, engine, profile):
        self.engine, self.profile = engine, profile

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def generate(self, text, *args, **options):
        return self.engine.generate(self.profile.apply(text), *args, **options)
