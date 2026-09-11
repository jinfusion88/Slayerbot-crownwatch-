"""Pulls deck info from Moxfield (name, commander, colors, and art) so a claim post
shows more than a plain link.

I import aiohttp inside fetch_moxfield_deck instead of at the top so the parsing
functions can still be tested on a machine without aiohttp. If anything goes wrong
the fetch just returns None and the bot posts the raw link instead.
"""
import logging
import re
from typing import Optional

logger = logging.getLogger('discord')

MOXFIELD_API_BASE = "https://api2.moxfield.com/v2/decks/all/"
MOXFIELD_TIMEOUT_SECONDS = 5.0
MOXFIELD_USER_AGENT = "SlayerBot/1.0 (cEDH title tracker; contact: server admin)"
DECK_URL_PATTERN = re.compile(r"moxfield\.com/decks/([A-Za-z0-9_-]+)", re.IGNORECASE)

COLOR_NAMES = {
    "W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green",
}
WUBRG_ORDER = "WUBRG"

# Names for all 32 color identities. The keys have to be in WUBRG order because
# that's what normalize_color_identity returns, otherwise the lookup never matches.
COLOR_IDENTITY_NAMES = {
    "": "Colorless",
    "W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green",
    # two color
    "WU": "Azorius", "UB": "Dimir", "BR": "Rakdos", "RG": "Gruul", "WG": "Selesnya",
    "WB": "Orzhov", "UR": "Izzet", "BG": "Golgari", "WR": "Boros", "UG": "Simic",
    # three color
    "WUG": "Bant", "WUB": "Esper", "UBR": "Grixis", "BRG": "Jund", "WRG": "Naya",
    "WBG": "Abzan", "WUR": "Jeskai", "UBG": "Sultai", "WBR": "Mardu", "URG": "Temur",
    # four color
    "WUBR": "Chaos", "UBRG": "Aggression", "WBRG": "Altruism", "WURG": "Growth", "WUBG": "Artifice",
    # five color
    "WUBRG": "WUBRG",
}


def extract_deck_id(deck_url: str) -> Optional[str]:
    """Gets the deck id out of a Moxfield deck URL, or None if it isn't one."""
    if not deck_url:
        return None
    match = DECK_URL_PATTERN.search(deck_url)
    if not match:
        return None
    return match.group(1)


def is_moxfield_url(deck_url: Optional[str]) -> bool:
    return extract_deck_id(deck_url) is not None


def _coerce_color_letters(colors) -> set:
    """Turns a color identity value into a set of letters. Returns an empty set
    if Moxfield sends something weird instead of crashing.
    """
    if not colors:
        return set()
    try:
        return {str(letter).upper() for letter in colors}
    except TypeError:
        return set()


def _coerce_str(value) -> str:
    """Makes sure a payload value is a string, "" if it is missing."""
    return str(value) if value else ""


def normalize_color_identity(colors) -> str:
    """Sorts color letters into WUBRG order, e.g. ["B","U","R"] -> "UBR"."""
    letters = _coerce_color_letters(colors)
    return "".join(letter for letter in WUBRG_ORDER if letter in letters)


def format_color_identity(identity: str) -> str:
    """Turns a WUBRG string into a name like "Grixis". Unknown ones become "3-Color" etc."""
    # The database can hand me None here, not just "", so I check for both.
    if not identity:
        return "Colorless"
    if identity in COLOR_IDENTITY_NAMES:
        return COLOR_IDENTITY_NAMES[identity]
    return f"{len(identity)}-Color"


def _extract_commander_cards(payload: dict) -> list:
    # Moxfield sometimes sends commanders as a dict and sometimes as a list,
    # so I handle both and just return a list of card dicts.
    commanders = payload.get("commanders")
    if isinstance(commanders, dict):
        entries = commanders.values()
    elif isinstance(commanders, list):
        entries = commanders
    else:
        return []
    cards = []
    for entry in entries:
        if isinstance(entry, dict):
            card = entry.get("card")
            if isinstance(card, dict):
                cards.append(card)
    return cards


def parse_moxfield_payload(payload: dict, deck_url: str) -> Optional[dict]:
    """Converts the Moxfield JSON into the dict the bot uses:
    name, commander_name, color_identity, author, public_url, image_url.
    Missing fields come back as "". Returns None if there's no name and no commander.
    """
    if not isinstance(payload, dict):
        return None

    name = _coerce_str(payload.get("name"))

    created_by = payload.get("createdByUser")
    author = ""
    if isinstance(created_by, dict):
        author = _coerce_str(created_by.get("userName") or created_by.get("displayName"))

    commander_cards = _extract_commander_cards(payload)
    commander_name = " // ".join(
        _coerce_str(card.get("name")) for card in commander_cards if card.get("name")
    )

    if not name and not commander_name:
        return None

    raw_identity = payload.get("colorIdentity")
    if raw_identity is not None:
        color_identity = normalize_color_identity(raw_identity)
    else:
        union = set()
        for card in commander_cards:
            union.update(_coerce_color_letters(card.get("colorIdentity")))
        color_identity = normalize_color_identity(union)

    image_url = ""
    if commander_cards:
        first_card = commander_cards[0]
        image_uris = first_card.get("image_uris")
        if isinstance(image_uris, dict):
            image_url = image_uris.get("art_crop") or image_uris.get("normal") or ""
        if not image_url and first_card.get("id"):
            image_url = f"https://assets.moxfield.net/cards/card-{first_card['id']}-art_crop.jpg"

    # I build the URL from the id I asked for instead of trusting the payload.
    deck_id = extract_deck_id(deck_url)
    public_url = f"https://www.moxfield.com/decks/{deck_id}" if deck_id else ""

    return {
        "name": name,
        "commander_name": commander_name,
        "color_identity": color_identity,
        "author": author,
        "public_url": public_url,
        "image_url": image_url,
    }


async def fetch_moxfield_deck(deck_url: str) -> Optional[dict]:
    """Fetches and parses one deck. Returns None on any failure so it never breaks a claim."""
    deck_id = extract_deck_id(deck_url)
    if not deck_id:
        return None

    try:
        import aiohttp
    except ImportError:
        logger.warning("aiohttp not installed; skipping Moxfield deck fetch for %s", deck_url)
        return None

    timeout = aiohttp.ClientTimeout(total=MOXFIELD_TIMEOUT_SECONDS)
    headers = {"User-Agent": MOXFIELD_USER_AGENT, "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(MOXFIELD_API_BASE + deck_id) as response:
                if response.status != 200:
                    logger.info("Moxfield API returned status %s for deck %s", response.status, deck_id)
                    return None
                payload = await response.json(content_type=None)
    except Exception:
        # Catching everything on purpose. A network problem should just fall back
        # to the plain link, not break the verification.
        logger.exception("Moxfield deck fetch failed for %s", deck_url)
        return None

    return parse_moxfield_payload(payload, deck_url)
