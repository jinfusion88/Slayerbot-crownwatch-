"""Tests for moxfield_api.py. These only test the parsing and URL functions,
nothing here makes a network call.
"""
import sys

from moxfield_api import (
    extract_deck_id,
    is_moxfield_url,
    normalize_color_identity,
    format_color_identity,
    parse_moxfield_payload,
)

VALID_DECK_URLS = [
    "https://www.moxfield.com/decks/AbC-123_x",
    "http://moxfield.com/decks/AbC-123_x/",
    "https://moxfield.com/decks/AbC-123_x?utm=1",
    "https://www.moxfield.com/decks/AbC-123_x#primer",
]

NON_DECK_URLS = [
    None,
    "",
    "not a url",
    "https://archidekt.com/decks/12345",
    "https://www.moxfield.com/users/bob",
]


def test_extract_deck_id_variants_all_resolve_to_same_id():
    for url in VALID_DECK_URLS:
        assert extract_deck_id(url) == "AbC-123_x"


def test_extract_deck_id_returns_none_for_non_deck_urls():
    for url in NON_DECK_URLS:
        assert extract_deck_id(url) is None


def test_is_moxfield_url_agrees_with_extract_deck_id():
    for url in VALID_DECK_URLS:
        assert is_moxfield_url(url) is True
    for url in NON_DECK_URLS:
        assert is_moxfield_url(url) is False


def test_normalize_color_identity():
    assert normalize_color_identity(["B", "U", "R"]) == "UBR"
    assert normalize_color_identity("rub") == "UBR"
    assert normalize_color_identity(["U", "U", "B"]) == "UB"
    assert normalize_color_identity([]) == ""
    assert normalize_color_identity(None) == ""


def test_format_color_identity():
    assert format_color_identity("") == "Colorless"
    assert format_color_identity("U") == "Blue"
    assert format_color_identity("UBR") == "Grixis"
    assert format_color_identity("URG") == "Temur"
    assert format_color_identity("WUBR") == "Chaos"
    assert format_color_identity("WUBRG") == "WUBRG"


def test_format_color_identity_falls_back_for_an_identity_not_in_the_table():
    # "GUR" isn't in WUBRG order, so it shouldn't match Temur.
    assert format_color_identity("GUR") == "3-Color"


def test_format_color_identity_treats_none_as_colorless():
    # The database can return None for a color identity, it should show as Colorless.
    assert format_color_identity(None) == "Colorless"
    assert format_color_identity("") == "Colorless"


def test_format_color_identity_composes_with_normalize_color_identity():
    # Makes sure normalize and format stay in sync with each other.
    assert format_color_identity(normalize_color_identity(["G", "U", "R"])) == "Temur"
    assert format_color_identity(normalize_color_identity(["G", "W", "U", "B"])) == "Artifice"


def _commander_card(name, colors, card_id="card-1"):
    return {
        "card": {
            "name": name,
            "id": card_id,
            "colorIdentity": colors,
            "image_uris": {"art_crop": f"https://example.com/{card_id}-art.jpg"},
        }
    }


def test_parse_payload_with_dict_shaped_commanders():
    payload = {
        "name": "Meren Value Town",
        "createdByUser": {"userName": "some_pilot", "displayName": "Some Pilot"},
        "colorIdentity": ["B", "G"],
        "commanders": {
            "Meren of Clan Nel Toth": _commander_card("Meren of Clan Nel Toth", ["B", "G"]),
        },
    }
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/xyz789")
    assert result == {
        "name": "Meren Value Town",
        "commander_name": "Meren of Clan Nel Toth",
        "color_identity": "BG",
        "author": "some_pilot",
        "public_url": "https://www.moxfield.com/decks/xyz789",
        "image_url": "https://example.com/card-1-art.jpg",
    }


def test_parse_payload_with_list_shaped_commanders_matches_dict_shape():
    dict_payload = {
        "name": "Meren Value Town",
        "createdByUser": {"userName": "some_pilot"},
        "colorIdentity": ["B", "G"],
        "commanders": {
            "Meren of Clan Nel Toth": _commander_card("Meren of Clan Nel Toth", ["B", "G"]),
        },
    }
    list_payload = dict(dict_payload)
    list_payload["commanders"] = [_commander_card("Meren of Clan Nel Toth", ["B", "G"])]

    dict_result = parse_moxfield_payload(dict_payload, "https://www.moxfield.com/decks/xyz789")
    list_result = parse_moxfield_payload(list_payload, "https://www.moxfield.com/decks/xyz789")
    assert dict_result == list_result


def test_parse_payload_joins_multiple_commanders_with_double_slash():
    payload = {
        "name": "Partner Deck",
        "colorIdentity": ["W", "B"],
        "commanders": [
            _commander_card("Kraum, Ludevic's Opus", ["U", "R"], card_id="kraum"),
            _commander_card("Tymna the Weaver", ["W", "B"], card_id="tymna"),
        ],
    }
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/partner1")
    assert result["commander_name"] == "Kraum, Ludevic's Opus // Tymna the Weaver"


def test_parse_payload_returns_none_for_bad_shapes_without_raising():
    assert parse_moxfield_payload({}, "https://www.moxfield.com/decks/xyz") is None
    assert parse_moxfield_payload(None, "https://www.moxfield.com/decks/xyz") is None
    assert parse_moxfield_payload("a string", "https://www.moxfield.com/decks/xyz") is None


def test_parse_payload_with_name_but_no_commanders_gives_empty_string_not_none():
    payload = {"name": "Solitaire Storm", "colorIdentity": ["U"]}
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/solo1")
    assert result is not None
    assert result["commander_name"] == ""


def test_parse_payload_derives_color_identity_from_commander_cards_when_top_level_absent():
    payload = {
        "name": "No Top-Level Identity",
        "commanders": {
            "Korvold, Fae-Cursed King": _commander_card("Korvold, Fae-Cursed King", ["B", "R", "G"]),
        },
    }
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/korvold1")
    assert result["color_identity"] == "BRG"


def test_public_url_is_always_rebuilt_from_the_input_url():
    payload = {
        "name": "Trust No One",
        "colorIdentity": ["R"],
        "publicUrl": "https://www.moxfield.com/decks/some-other-id",
    }
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/real-id")
    assert result["public_url"] == "https://www.moxfield.com/decks/real-id"


def test_parse_payload_survives_malformed_top_level_color_identity():
    # A number instead of a list shouldn't crash.
    payload = {"name": "Weird Deck", "colorIdentity": 5}
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/weird1")
    assert result is not None
    assert result["color_identity"] == ""


def test_parse_payload_survives_malformed_commander_card_color_identity():
    # Same thing but through the commander card path.
    payload = {
        "name": "X",
        "commanders": {
            "X": {"card": {"name": "X", "id": "x1", "colorIdentity": 5}},
        },
    }
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/weird2")
    assert result is not None
    assert result["color_identity"] == ""


def test_parse_payload_coerces_non_string_name_and_commander_name():
    payload = {
        "name": 123,
        "commanders": {
            "X": {"card": {"name": 456, "id": "x1", "colorIdentity": ["U"]}},
        },
    }
    result = parse_moxfield_payload(payload, "https://www.moxfield.com/decks/weird3")
    assert result is not None
    assert result["name"] == "123"
    assert result["commander_name"] == "456"


def test_lazy_import_does_not_pull_in_aiohttp():
    # Importing moxfield_api shouldn't import aiohttp. If aiohttp is installed
    # anyway I skip the check.
    import importlib.util
    if importlib.util.find_spec("aiohttp") is not None:
        return
    assert "aiohttp" not in sys.modules
