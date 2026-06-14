"""Champion name resolution.

League scoreboards show champions as portrait icons, not text, so OCR can't read
them — champions are entered/confirmed by the admin. To avoid typing full names,
this module resolves loose input ("mf", "leblanc", "kaisa", "dr mundo") to the
canonical champion name via:

  1. exact match on the normalized canonical name,
  2. a hand-maintained alias map (community shorthands),
  3. a fuzzy (difflib) guess above a cutoff.

`normalize()` strips everything but letters/digits and lowercases, so
punctuation/spacing variants ("Kai'Sa", "kaisa", "KAI SA") all collapse together.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

# Canonical Riot champion names. Keep this list current as champions release;
# a missing champ just falls through to fuzzy/manual entry rather than erroring.
CHAMPIONS: list[str] = [
    "Aatrox", "Ahri", "Akali", "Akshan", "Alistar", "Ambessa", "Amumu", "Anivia",
    "Annie", "Aphelios", "Ashe", "Aurelion Sol", "Aurora", "Azir", "Bard",
    "Bel'Veth", "Blitzcrank", "Brand", "Braum", "Briar", "Caitlyn", "Camille",
    "Cassiopeia", "Cho'Gath", "Corki", "Darius", "Diana", "Dr. Mundo", "Draven",
    "Ekko", "Elise", "Evelynn", "Ezreal", "Fiddlesticks", "Fiora", "Fizz",
    "Galio", "Gangplank", "Garen", "Gnar", "Gragas", "Graves", "Gwen", "Hecarim",
    "Heimerdinger", "Hwei", "Illaoi", "Irelia", "Ivern", "Janna", "Jarvan IV",
    "Jax", "Jayce", "Jhin", "Jinx", "K'Sante", "Kai'Sa", "Kalista", "Karma",
    "Karthus", "Kassadin", "Katarina", "Kayle", "Kayn", "Kennen", "Kha'Zix",
    "Kindred", "Kled", "Kog'Maw", "LeBlanc", "Lee Sin", "Leona", "Lillia",
    "Lissandra", "Lucian", "Lulu", "Lux", "Malphite", "Malzahar", "Maokai",
    "Master Yi", "Mel", "Milio", "Miss Fortune", "Mordekaiser", "Morgana",
    "Naafiri", "Nami", "Nasus", "Nautilus", "Neeko", "Nidalee", "Nilah",
    "Nocturne", "Nunu & Willump", "Olaf", "Orianna", "Ornn", "Pantheon", "Poppy",
    "Pyke", "Qiyana", "Quinn", "Rakan", "Rammus", "Rek'Sai", "Rell",
    "Renata Glasc", "Renekton", "Rengar", "Riven", "Rumble", "Ryze", "Samira",
    "Sejuani", "Senna", "Seraphine", "Sett", "Shaco", "Shen", "Shyvana",
    "Singed", "Sion", "Sivir", "Skarner", "Smolder", "Sona", "Soraka", "Swain",
    "Sylas", "Syndra", "Tahm Kench", "Taliyah", "Talon", "Taric", "Teemo",
    "Thresh", "Tristana", "Trundle", "Tryndamere", "Twisted Fate", "Twitch",
    "Udyr", "Urgot", "Varus", "Vayne", "Veigar", "Vel'Koz", "Vex", "Vi", "Viego",
    "Viktor", "Vladimir", "Volibear", "Warwick", "Wukong", "Xayah", "Xerath",
    "Xin Zhao", "Yasuo", "Yone", "Yorick", "Yunara", "Yuumi", "Zaahen", "Zac",
    "Zed", "Zeri", "Ziggs", "Zilean", "Zoe", "Zyra",
]

# Community shorthands -> canonical name. Keys are normalized on load, so case
# and punctuation here don't matter.
_ALIASES_RAW: dict[str, str] = {
    "mf": "Miss Fortune",
    "ww": "Warwick",
    "j4": "Jarvan IV",
    "jarvan": "Jarvan IV",
    "tf": "Twisted Fate",
    "asol": "Aurelion Sol",
    "kha": "Kha'Zix",
    "cho": "Cho'Gath",
    "kog": "Kog'Maw",
    "vel": "Vel'Koz",
    "rek": "Rek'Sai",
    "reksai": "Rek'Sai",
    "mundo": "Dr. Mundo",
    "yi": "Master Yi",
    "gp": "Gangplank",
    "lb": "LeBlanc",
    "ali": "Alistar",
    "blitz": "Blitzcrank",
    "eve": "Evelynn",
    "fiddle": "Fiddlesticks",
    "fiddles": "Fiddlesticks",
    "heca": "Hecarim",
    "heim": "Heimerdinger",
    "kass": "Kassadin",
    "kat": "Katarina",
    "malz": "Malzahar",
    "morde": "Mordekaiser",
    "morg": "Morgana",
    "naut": "Nautilus",
    "nunu": "Nunu & Willump",
    "ori": "Orianna",
    "panth": "Pantheon",
    "renek": "Renekton",
    "sej": "Sejuani",
    "sera": "Seraphine",
    "tahm": "Tahm Kench",
    "trist": "Tristana",
    "trynd": "Tryndamere",
    "voli": "Volibear",
    "monkeyking": "Wukong",
    "xin": "Xin Zhao",
    "ksante": "K'Sante",
    "kaisa": "Kai'Sa",
    "belveth": "Bel'Veth",
    "renata": "Renata Glasc",
    "sol": "Aurelion Sol",
    "yummi": "Yuumi",
    "ww_": "Warwick",
}


def normalize(s: str) -> str:
    """Collapse a champion name to its match key: letters+digits only, lowercased.
    'Kai'Sa' -> 'kaisa', 'Dr. Mundo' -> 'drmundo', "Nunu & Willump" -> 'nunuwillump'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Built once at import: normalized canonical name -> canonical, plus aliases.
_CANON_BY_NORM: dict[str, str] = {normalize(c): c for c in CHAMPIONS}
_ALIAS_BY_NORM: dict[str, str] = {normalize(k): v for k, v in _ALIASES_RAW.items()}

# Fuzzy match must clear this difflib ratio to be returned as a confident hit.
_FUZZY_CUTOFF = 0.82


def resolve_champion(text: str) -> Optional[str]:
    """Resolve loose input to a canonical champion name, or None if no confident
    match. Tries exact-normalized, then alias, then a single fuzzy guess."""
    if not text or not text.strip():
        return None
    key = normalize(text)
    if not key:
        return None
    if key in _CANON_BY_NORM:
        return _CANON_BY_NORM[key]
    if key in _ALIAS_BY_NORM:
        return _ALIAS_BY_NORM[key]
    match = difflib.get_close_matches(key, list(_CANON_BY_NORM.keys()), n=1, cutoff=_FUZZY_CUTOFF)
    if match:
        return _CANON_BY_NORM[match[0]]
    return None


def suggest_champions(text: str, n: int = 5) -> list[str]:
    """Best-effort ranked suggestions for autocomplete / 'did you mean'. Returns
    canonical names. Prefix matches first, then fuzzy-ranked remainder."""
    key = normalize(text)
    if not key:
        return CHAMPIONS[:n]
    exact = [c for c in CHAMPIONS if normalize(c) == key]
    prefix = [c for c in CHAMPIONS if normalize(c).startswith(key) and c not in exact]
    contains = [
        c for c in CHAMPIONS
        if key in normalize(c) and c not in exact and c not in prefix
    ]
    ranked = exact + prefix + contains
    if len(ranked) < n:
        fuzzy_keys = difflib.get_close_matches(
            key, list(_CANON_BY_NORM.keys()), n=n, cutoff=0.6
        )
        for fk in fuzzy_keys:
            c = _CANON_BY_NORM[fk]
            if c not in ranked:
                ranked.append(c)
    return ranked[:n]
