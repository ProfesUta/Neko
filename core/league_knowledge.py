"""Small, patch-pinned Data Dragon retrieval. No polling, vision or LLM calls.

Cache misses may download on the conversation worker, never on the GUI thread.
Prewarm before playing with: python -m core.league_knowledge --champions LeeSin Yuumi
"""
import argparse
import copy
import html
import json
import re
import tempfile
import struct
from functools import lru_cache
from pathlib import Path
from threading import Lock

import httpx

import config
from core.cancellation import check_cancelled

SOURCE = "https://ddragon.leagueoflegends.com"
GUIDE = "https://www.leagueoflegends.com/en-us/how-to-play/"


def plain(value, limit=1200):
    text = re.sub(r"<[^>]*>", " ", str(value or ""))
    text = re.sub(r"\{\{.*?\}\}", "[unresolved scaling]", text)
    text = " ".join(html.unescape(text).split())
    return text if len(text)<=limit else text[:limit].rsplit(" ",1)[0].rstrip(" ,;:") + "…"


@lru_cache(maxsize=2048)  # Current catalog has >500 distinct item names plus champions.
def name_pattern(name):
    return re.compile(r"(?<!\w)" + re.escape(name).replace(r"\ ", r"\s+").replace("'","['\u2019]") + r"(?:['\u2019]s)?(?!\w)", re.I)


class LeagueKnowledge:
    def __init__(self, names, root=None, http=None):
        self.names = names
        self.root = Path(root or config.DATA_DIR / "league-knowledge")
        self.version = config.LEAGUE_KNOWLEDGE_PATCH
        self._memory = {}
        self._lock, self._download = Lock(), Lock()
        self._http = http
        self._last_reference = None
        self._retry_after = 0.0
        self._item_index = None
        self._local_names = None
        if not self.version:
            manifest = self._disk("current.json", 1024) or {}
            self.version = manifest.get("version", "")
        if self.version and not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            self.version = ""

    def _disk(self, key, bound=8_000_000):
        try:
            path = self.root / key
            if path.stat().st_size <= bound:
                value = json.loads(path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            pass
        return None

    def _save(self, key, value):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as target:
                temporary = Path(target.name)
                json.dump(value, target, ensure_ascii=False)
            temporary.replace(self.root / key)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _fetch(self, path, cancel_event=None):
        check_cancelled(cancel_event)
        if self._http is None:
            self._http = httpx.Client(timeout=1.5, trust_env=False, follow_redirects=False,
                                      limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        with self._http.stream("GET", SOURCE + path) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                check_cancelled(cancel_event)
                size += len(chunk)
                if size > 8_000_000:
                    raise ValueError("Oversized static League reference")
                chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def _version(self, cancel_event=None, refresh=False):
        if self.version and not refresh:
            return self.version
        versions = self._fetch("/api/versions.json", cancel_event)
        value = versions[0] if isinstance(versions, list) and versions else ""
        if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ValueError("Invalid Data Dragon patch")
        self.version = value
        self._save("current.json", {"version": value})
        return value

    def data(self, kind, champion=None, cancel_event=None):
        if kind not in ("champion", "item") or champion and not re.fullmatch(r"[A-Za-z0-9]+", champion):
            raise ValueError("Invalid static reference key")
        suffix = f"champion-{champion}" if champion else kind
        key = f"{self.version}-{suffix}.json"
        with self._lock:
            cached = self._memory.get(key)
        if cached is not None:
            return cached
        cached = self._disk(key)
        if cached is not None and not isinstance(cached.get("data"),dict):
            cached = None
        if cached is not None:
            with self._lock:
                self._memory[key] = cached
            return cached
        import time
        if not config.LEAGUE_STATIC_DOWNLOADS_ENABLED or time.monotonic() < self._retry_after:
            return None
        while not self._download.acquire(timeout=.05):
            check_cancelled(cancel_event)
        try:
            version = self._version(cancel_event)
            key = f"{version}-{suffix}.json"
            with self._lock:
                cached = self._memory.get(key)
            if cached is not None:
                return cached
            cached = self._disk(key)
            if cached is not None and not isinstance(cached.get("data"),dict):
                cached = None
            if cached is None:
                path = f"champion/{champion}" if champion else kind
                cached = self._fetch(f"/cdn/{version}/data/en_US/{path}.json", cancel_event)
                if not isinstance(cached, dict) or not isinstance(cached.get("data"), dict):
                    raise ValueError("Invalid Data Dragon reference")
                self._save(key, cached)
            with self._lock:
                self._memory[key] = cached
                if kind == "item":
                    self._item_index = cached
            return cached
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            self._retry_after = time.monotonic() + 30
            return None
        finally:
            self._download.release()

    def champion_icon(self, champion, cancel_event=None, cache_root=None):
        """Explicit offline preparation only; pinned public icon, cached and bounded."""
        if not isinstance(champion,str):raise ValueError("Champion name must be text")
        index=self.data("champion",cancel_event=cancel_event)
        if not index:raise ValueError("Champion catalog unavailable; prewarm the static League cache")
        entry=next((item for key,item in index["data"].items() if isinstance(item,dict)
                    and champion.casefold() in (str(key).casefold(),str(item.get("name","")).casefold())),None)
        image_info=(entry or {}).get("image",{})
        filename=image_info.get("full","") if isinstance(image_info,dict) else ""
        if not re.fullmatch(r"[A-Za-z0-9]+\.png",filename):
            raise ValueError(f"No official champion icon for {champion!r}")
        version=self.version
        path=Path(cache_root or self.root/"icons")/f"riot-{version}"/filename
        def valid(data):
            return (len(data)>=33 and data[:8]==b"\x89PNG\r\n\x1a\n" and data[12:16]==b"IHDR"
                    and all(6<=n<=512 for n in struct.unpack(">II",data[16:24])))
        def cached():
            try:return path.stat().st_size<=1_000_000 and valid(path.read_bytes())
            except OSError:return False
        check_cancelled(cancel_event)
        if cached():return path
        if not config.LEAGUE_STATIC_DOWNLOADS_ENABLED:
            raise ValueError(f"Official icon for {champion} is not cached and static downloads are disabled")
        while not self._download.acquire(timeout=.05):check_cancelled(cancel_event)
        temporary=None
        try:
            check_cancelled(cancel_event)
            if cached():return path
            if self._http is None:
                self._http=httpx.Client(timeout=1.5,trust_env=False,follow_redirects=False,
                                        limits=httpx.Limits(max_connections=1,max_keepalive_connections=1))
            data=bytearray()
            with self._http.stream("GET",SOURCE+f"/cdn/{version}/img/champion/{filename}") as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    check_cancelled(cancel_event)
                    data.extend(chunk)
                    if len(data)>1_000_000:raise ValueError("Oversized champion icon")
            if not valid(data):raise ValueError("Official champion reference is not a supported PNG")
            path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as target:
                temporary=Path(target.name);target.write(data)
            check_cancelled(cancel_event)
            temporary.replace(path)
            return path
        except httpx.HTTPError as error:
            raise ValueError(f"Official champion icon download failed for {champion}; existing cached references were retained") from error
        finally:
            if temporary:temporary.unlink(missing_ok=True)
            self._download.release()

    def champions_in(self, text):
        if self._local_names is None:
            index = self._disk(f"{self.version}-champion.json") or {}
            self._local_names = tuple(item["name"] for item in index.get("data",{}).values()
                                      if isinstance(item,dict) and isinstance(item.get("name"),str))
        values = (*self.names.names.values(),*self._local_names)
        return tuple(name for name in sorted(set(values), key=len, reverse=True) if name_pattern(name).search(text))[:3]

    def matches(self, text, champion_hint=None):
        if re.search(r"\b(?:why did (?:i|we)|what did i (?:do|miss))\b",text,re.I):
            return False  # A static tip cannot explain an unobserved personal play.
        topic = re.search(r"\b(abilit\w*|passive|spell|strengths?|weakness\w*|matchup|interact\w*|does|do|role|class|[qwer]|counter|against)\b", text, re.I)
        if topic and (champion_hint or self.champions_in(text) or re.search(r"\b(league|lol|champion|enemy jungler)\b", text, re.I)):
            return True
        if re.search(r"\b(jungle path\w*|objective mechanics|(?:how|what) (?:does|do) (?:the )?(?:dragon|baron|turret|inhibitor)|league item)\b", text, re.I):
            return True
        if re.search(r"\bwhat does .+ do\b|\bitem effect", text, re.I):
            if self._item_index is None:
                self._item_index = self._disk(f"{self.version}-item.json") or {}
            return any(isinstance(item, dict) and isinstance(item.get("name"), str) and name_pattern(item["name"]).search(text)
                       for item in self._item_index.get("data", {}).values())
        return False

    def answer(self, text, champion_hint=None, cancel_event=None, item_ids=(), map_id=None):
        if not config.LEAGUE_KNOWLEDGE_ENABLED or not self.matches(text, champion_hint):
            return None
        check_cancelled(cancel_event)
        if re.search(r"\b(?:jungle path\w*|pathing)\b", text, re.I):
            return ("General pathing means choosing an order of jungle camps while considering travel, resources and objectives. "
                    "A last sighting does not reveal a jungler's route or current location. I won't predict their route.")
        if re.search(r"\b(?:objective mechanics|(?:how|what) (?:does|do) (?:the )?(?:dragon|baron|turret|inhibitor))\b", text, re.I):
            return ("Riot's general guide describes dragons and Baron as neutral monsters that grant team buffs. "
                    "Turrets and inhibitors protect the path to the Nexus. Exact effects and timings need patch-specific data.")
        champions = self.champions_in(text)
        if not champions and champion_hint:
            champions = (champion_hint,)
        index = self.data("champion", cancel_event=cancel_event)
        if not index:
            return "I don't have a verified cached League reference available. I won't guess the ability or item effect."
        entries = index["data"]
        resolved = []
        for name in champions[:2]:
            match = next(((key, value) for key, value in entries.items() if isinstance(value, dict)
                          and value.get("name", "").casefold() == name.casefold()), None)
            if match:
                resolved.append(match)
        if not resolved:
            items = self.data("item", cancel_event=cancel_event)
            matches = [(key, item) for key, item in (items or {}).get("data", {}).items()
                       if isinstance(item, dict) and isinstance(item.get("name"), str) and name_pattern(item["name"]).search(text)]
            inventory = [(key,item) for key,item in matches if key in {str(value) for value in item_ids}]
            if inventory:
                matches = inventory
            elif map_id is not None:
                matches = [(key,item) for key,item in matches if item.get("maps",{}).get(str(map_id)) is True]
            if len(matches)>1:
                variants = {json.dumps({key:item.get(key) for key in ("description","stats","effect")},sort_keys=True)
                            for _,item in matches}
                if len(variants)==1:
                    matches=matches[:1]
                else:
                    return "That item name has different game-mode versions. Which mode are you asking about? I won't choose an effect arbitrarily."
            if len(matches) != 1:
                return "Which champion, ability or item do you mean? I can't identify one reference confidently."
            key, item = matches[0]
            fact = {"item": item["name"], "effect": plain(item.get("description"), 900),
                    "patch": self.version, "source": SOURCE + f"/cdn/{self.version}/data/en_US/item.json"}
            self._reference(text, [fact])
            return f"Data Dragon patch {self.version}: {fact['item']}. {fact['effect']}"
        facts, pieces = [], []
        for key, brief in resolved:
            detail = self.data("champion", key, cancel_event)
            item = (detail or {}).get("data", {}).get(key)
            if not isinstance(item, dict):
                return f"I know the champion name, but I couldn't verify the current cached abilities for {brief.get('name', key)}."
            facts.append(self._champion_reference(key, item, text))
            fact = facts[-1]
            if re.search(r"\b(role|class)\b", text, re.I):
                pieces.append(f"{fact['champion']}: Data Dragon classes are {', '.join(fact['classes'])}. These are not assigned lane roles.")
            elif re.search(r"\b(strength|weak|matchup|counter|against)\w*\b", text, re.I):
                tips = fact["weakness_tips"] if re.search(r"weak|counter|against", text, re.I) else fact["playing_tips"]
                pieces.append(f"{fact['champion']}: " + (" ".join(tips[:2]) or "The reference doesn't supply verified tips."))
            else:
                pieces.append(f"{fact['champion']}: " + "; ".join(f"{ability['slot']}, {ability['name']}: {ability['description']}" for ability in fact["abilities"]))
        self._reference(text, facts)
        if re.search(r"interact|block|cancel|immune|work (?:on|with|against)", text, re.I):
            return "The cached descriptions don't verify that exact interaction. " + " ".join(pieces)[:1300]
        return f"Data Dragon patch {self.version}: " + " ".join(pieces)[:1800]

    def _champion_reference(self, key, item, text):
        slots = [("Passive", item.get("passive", {})), *zip("QWER", item.get("spells", [])[:4])]
        requested = {match[0].casefold() for match in re.finditer(r"\b(passive|[qwer])\b", text, re.I)}
        abilities = [{"slot": slot, "name": plain(ability.get("name"), 100),
                      "description": plain(ability.get("description"), 350)}
                     for slot, ability in slots if isinstance(ability, dict)
                     and (not requested or slot.casefold() in requested)]
        return {"champion": plain(item.get("name"), 80), "patch": self.version,
                "source": SOURCE + f"/cdn/{self.version}/data/en_US/champion/{key}.json",
                "classes": item.get("tags", [])[:3], "abilities": abilities,
                "playing_tips": [plain(tip, 260) for tip in item.get("allytips", [])[:2]],
                "weakness_tips": [plain(tip, 260) for tip in item.get("enemytips", [])[:2]]}

    def _reference(self, question, facts):
        with self._lock:
            self._last_reference = (question, facts)

    def context_for(self, text):
        with self._lock:
            return copy.deepcopy(self._last_reference[1]) if self._last_reference and self._last_reference[0] == text else None

    def close(self):
        with self._download:
            if self._http is not None:
                self._http.close()
                self._http = None


def main():
    from core.league_champ_select import ChampionNames
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--champions", nargs="*", default=[])
    parser.add_argument("--refresh", action="store_true", help="Explicitly move cache to the latest published patch")
    args = parser.parse_args()
    knowledge = LeagueKnowledge(ChampionNames())
    try:
        if args.refresh:
            knowledge._version(refresh=True)
        index = knowledge.data("champion")
        items = knowledge.data("item")
        if index is None or items is None:
            parser.exit(1, "Static reference download failed; existing cache retained.\n")
        for key in args.champions:
            if key not in index["data"] or knowledge.data("champion", key) is None:
                parser.exit(1, "Unknown/unavailable champion ID; use Data Dragon IDs such as LeeSin.\n")
        print(f"Cached Data Dragon patch {knowledge.version}; {len(args.champions)} detailed champion references.")
    finally:
        knowledge.close()


if __name__ == "__main__":
    main()
