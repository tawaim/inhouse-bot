"""Local champion-icon recognition — no external API, no per-use cost.

Champion portraits are a fixed, known set, so we recognize them by matching a
cropped portrait against a library of perceptual hashes:

  1. SEED library (zero labeling): the official champion square icons from Riot's
     Data Dragon CDN, hashed once and committed to the repo. Works on day one.
  2. LEARNED library (self-improving): every admin-confirmed champion crop from a
     real screenshot is hashed and added, so matching adapts to this server's
     screenshot style/resolution over time.

Matching is nearest-neighbor by Hamming distance over a dHash — fast, tiny, and
CPU-only (fits the Fly worker). No torch, no GPU, no training run.

Build/refresh the seed library:
    python -m bot.services.champion_vision
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from PIL import Image

from bot.services.champions import resolve_champion

log = logging.getLogger(__name__)

# Committed seed hashes live here (NOT under any `data/` dir — that's gitignored).
SEED_PATH = Path(__file__).resolve().parent.parent / "assets" / "champion_hashes.json"
# Raw downloaded icons are cached here for future crop/match experiments (gitignored).
ICON_CACHE_DIR = Path(__file__).resolve().parent.parent / "assets" / "champion_icons"

_DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
_DDRAGON_CHAMPS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
_DDRAGON_ICON = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{img}"

# dHash grid: 16 → 256-bit hash (64 hex chars). Big enough to discriminate ~165
# champions, small enough that Hamming distance stays cheap.
HASH_SIZE = 16


# ---------------------------------------------------------------------------
# Perceptual hash (difference hash) — self-contained, no imagehash dependency.
# ---------------------------------------------------------------------------

def dhash(image: Image.Image, hash_size: int = HASH_SIZE) -> str:
    """Difference hash of an image, returned as a hex string. Compares each pixel
    to its right neighbor on a grayscale (hash_size+1 × hash_size) thumbnail, so
    it captures gradient structure and is robust to scale/brightness shifts."""
    img = image.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = list(img.getdata())
    w = hash_size + 1
    bits = 0
    n = 0
    for row in range(hash_size):
        base = row * w
        for col in range(hash_size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
            n += 1
    return f"{bits:0{(n + 3) // 4}x}"


def hamming(a_hex: str, b_hex: str) -> int:
    """Number of differing bits between two hex hashes (lower = more similar)."""
    return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")


# ---------------------------------------------------------------------------
# Library load / match
# ---------------------------------------------------------------------------

def load_library(path: Path = SEED_PATH) -> dict:
    """Load the hash library: {"version": str, "hashes": {champion: [hex, ...]}}.
    Values are LISTS so a champion can accumulate multiple reference hashes
    (the seed icon plus learned crops). Returns an empty library if missing."""
    if not path.exists():
        log.warning("Champion hash library not found at %s — run build_seed_library()", path)
        return {"version": None, "hashes": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Tolerate the seed format where each champion maps to a single hex string.
    for champ, val in list(data.get("hashes", {}).items()):
        if isinstance(val, str):
            data["hashes"][champ] = [val]
    return data


def match_champion(
    crop: Image.Image, library: dict, max_distance: int = 40
) -> tuple[Optional[str], int]:
    """Match a cropped champion portrait to the nearest library champion.
    Returns (canonical_name, distance), or (None, distance) if the best match is
    farther than max_distance (treated as 'not confident — ask the admin')."""
    h = dhash(crop)
    best_champ, best_dist = None, 1 << 30
    for champ, hashes in library.get("hashes", {}).items():
        for ref in hashes:
            d = hamming(h, ref)
            if d < best_dist:
                best_champ, best_dist = champ, d
    if best_champ is None or best_dist > max_distance:
        return None, best_dist
    return best_champ, best_dist


def add_learned_crop(champion: str, crop: Image.Image, path: Path = SEED_PATH) -> None:
    """Persist a confirmed champion crop's hash into the library so future matches
    improve. Deduplicates identical hashes. Called after an admin confirms a game."""
    canon = resolve_champion(champion) or champion
    lib = load_library(path)
    h = dhash(crop)
    bucket = lib["hashes"].setdefault(canon, [])
    if h not in bucket:
        bucket.append(h)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lib, f, indent=0)


# ---------------------------------------------------------------------------
# Seed builder — download Data Dragon icons, hash them, write the library.
# ---------------------------------------------------------------------------

def build_seed_library(
    dest: Path = SEED_PATH, cache_icons: bool = True
) -> dict:
    """Download the latest Data Dragon champion icons, hash each, and write the
    seed library to `dest`. Returns a summary dict. Requires network access (run
    offline, commit the resulting JSON — the bot never hits the network at runtime)."""
    import httpx

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        version = client.get(_DDRAGON_VERSIONS).json()[0]
        champ_data = client.get(_DDRAGON_CHAMPS.format(ver=version)).json()["data"]
        log.info("Data Dragon %s — %d champions", version, len(champ_data))

        if cache_icons:
            ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        hashes: dict[str, list[str]] = {}
        unresolved: list[str] = []
        for cid, info in sorted(champ_data.items()):
            img_name = info["image"]["full"]          # e.g. "MissFortune.png"
            ddragon_name = info["name"]               # e.g. "Miss Fortune"
            canon = resolve_champion(ddragon_name) or ddragon_name
            if resolve_champion(ddragon_name) is None:
                unresolved.append(ddragon_name)
            resp = client.get(_DDRAGON_ICON.format(ver=version, img=img_name))
            resp.raise_for_status()
            icon_path = ICON_CACHE_DIR / img_name
            if cache_icons:
                icon_path.write_bytes(resp.content)
            from io import BytesIO
            img = Image.open(BytesIO(resp.content))
            hashes[canon] = [dhash(img)]

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump({"version": version, "hashes": hashes}, f, indent=0)

    summary = {
        "version": version,
        "count": len(hashes),
        "unresolved": unresolved,
        "path": str(dest),
    }
    log.info("Seed library written: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = build_seed_library()
    print(f"\nData Dragon {result['version']}: hashed {result['count']} champions")
    print(f"Written to {result['path']}")
    if result["unresolved"]:
        print(f"\n[!] {len(result['unresolved'])} names not in champions.py "
              f"(stored under Data Dragon name): {', '.join(result['unresolved'])}")
