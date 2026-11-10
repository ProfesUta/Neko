"""Speech-only name normalization for Kokoro's dictionary-only frontend.

The project lexicon handles names missing from the creator's voice pack.
Displayed text and the original model/voice assets stay unchanged.
"""

import re


_NAMES = ("Bel'Veth", "Cho'Gath", "Kai'Sa", "Kha'Zix", "Kog'Maw", "K'Sante", "Rek'Sai", "Vel'Koz")
_APOSTROPHE_NAMES = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(name) for name in _NAMES) + r")(?!\w)", re.I)


def prepare_kokoro_text(text):
    # Exact champion phrases, not general substitutions for IV or ampersands.
    text = text.replace("\u2019", "'")
    text = re.sub(r"\bJarvan\s+IV\b", "Jarvan the Fourth", text, flags=re.I)
    text = re.sub(r"\bNunu\s*&\s*Willump\b", "Nunu and Willump", text, flags=re.I)
    return _APOSTROPHE_NAMES.sub(lambda match: match[0].replace("'", ""), text)
