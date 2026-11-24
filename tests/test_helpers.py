"""Tests of the parsing of the settings and of the feed, with answers shaped like the real ones."""

from typing import Any

import pytest

from radioplayer.helpers import (
    DEFAULT_POLL_SECONDS,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    STALE_AFTER_SECONDS,
    default_countries,
    parse_history,
    parse_item_id,
    parse_json,
    parse_now_playing,
    parse_search,
    parse_service,
    parse_services,
    parse_stations,
    poll_seconds,
    split_stream_title,
)

COVER = "https://assets.radioplayer.org/coverArt/96cd3aeaeb3c9b62f0c016f9cfa6c58d.jpg"
LOGO = "https://assets.radioplayer.org/056/0565067/600/600/l45bvtox.png"
START, STOP = 1795353896, 1795354103


def _events(now: dict[str, Any] | None, poll_next: Any = 6000) -> dict[str, Any]:
    """Return an answer of the feed with the given current item."""
    results: dict[str, Any] = {"previous": []}
    if now is not None:
        results["now"] = now
    return {"results": results, "total": 1, "pollNext": poll_next}


def _song(image: str = COVER) -> dict[str, Any]:
    """Return a song as the feed lists it."""
    return {
        "rpId": "5067",
        "name": "Arme Joe",
        "artistName": "NOORDKAAP",
        "serviceName": "Willy",
        "imageUrl": image,
        "startTime": str(START),
        "stopTime": str(STOP),
        "type": "PE_E",
        "song": True,
    }


# what the feed says between songs: the station, its logo and no song
BETWEEN_SONGS = {
    "type": "SI",
    "rpId": "5067",
    "serviceName": "Willy",
    "name": "",
    "artistName": "",
    "serviceDescription": "Music Matters",
    "startTime": "",
    "stopTime": "",
    "imageUrl": LOGO,
}


def test_song_with_cover() -> None:
    """A song shows its artist, title and cover."""
    metadata = parse_now_playing(_events(_song()), START + 10)
    assert metadata is not None
    assert (metadata.artist, metadata.title, metadata.image_url) == ("NOORDKAAP", "Arme Joe", COVER)


def test_song_with_only_the_logo() -> None:
    """A song whose picture is the station logo has no image, so the server finds a cover."""
    metadata = parse_now_playing(_events(_song(image=LOGO)), START + 10)
    assert metadata is not None
    assert metadata.title == "Arme Joe"
    assert metadata.image_url is None


def test_between_songs() -> None:
    """Between songs the station shows with its logo and no artist, whatever the stream says."""
    metadata = parse_now_playing(_events(BETWEEN_SONGS), START)
    assert metadata is not None
    assert (metadata.title, metadata.artist, metadata.image_url) == ("Willy", None, LOGO)


def test_ended_song() -> None:
    """A song the feed still lists well after its end shows nothing rather than a stale title."""
    assert parse_now_playing(_events(_song()), STOP + STALE_AFTER_SECONDS) is not None
    assert parse_now_playing(_events(_song()), STOP + STALE_AFTER_SECONDS + 1) is None


def test_feed_without_now() -> None:
    """An answer without a current item, or a broken one, says nothing."""
    assert parse_now_playing(_events(None), START) is None
    assert parse_now_playing({"results": []}, START) is None
    assert parse_now_playing({}, START) is None
    assert parse_now_playing(_events({**_song(), "stopTime": "soon"}), START) is not None


def test_history() -> None:
    """The songs before the current one come newest first, each once, without the breaks."""
    first = {**_song(), "startTime": "1", "name": "Old Song", "artistName": ""}
    events = _events(_song())
    events["results"]["previous"] = [first, BETWEEN_SONGS, _song(LOGO), _song(LOGO), "broken"]
    assert parse_history(events) == [
        (str(START), "NOORDKAAP", "Arme Joe", LOGO),
        ("1", None, "Old Song", COVER),
    ]
    # a song without a start time gets a key of its own, the same at every answer
    no_start = {**_song(), "startTime": "", "name": "No Start"}
    events["results"]["previous"] = [no_start]
    key = parse_history(events)[0][0]
    assert key.startswith("x")
    assert parse_history(events)[0][0] == key
    assert parse_history(_events(None)) == []
    assert parse_history(None) == []


def test_poll_seconds() -> None:
    """The pause the feed asks for, in milliseconds, is kept within bounds."""
    assert poll_seconds(_events(None, 15000)) == 15
    assert poll_seconds(_events(None, 6000)) == MIN_POLL_SECONDS
    assert poll_seconds(_events(None, 10**9)) == MAX_POLL_SECONDS
    assert poll_seconds(_events(None, "6000")) == DEFAULT_POLL_SECONDS
    assert poll_seconds(_events(None, poll_next=True)) == DEFAULT_POLL_SECONDS


def test_parse_stations() -> None:
    """Good lines become stations; each bad line is skipped with its reason."""
    stations, errors = parse_stations(
        [
            "Willy | https://stream.example/willy.mp3 | 056/5067",
            " Studio Brussel|https://stream.example/stubru.aac|056/5001| https://img.example/stubru.png ",
            "",
            "No id | https://stream.example/x.mp3",
            "Bad URL | ftp://stream.example/x.mp3 | 056/1",
            "Bad id | https://stream.example/x.mp3 | 5067",
            "Twice | https://stream.example/y.mp3 | 056/5067",
            "Bad logo | https://stream.example/z.mp3 | 056/2 | logo.png",
        ]
    )
    assert list(stations) == ["056-5067", "056-5001"]
    willy, stubru = stations.values()
    assert (willy.name, willy.country, willy.rp_id, willy.logo_url) == (
        "Willy",
        "056",
        "5067",
        None,
    )
    assert stubru.name == "Studio Brussel"
    assert stubru.stream_url == "https://stream.example/stubru.aac"
    assert stubru.logo_url == "https://img.example/stubru.png"
    assert len(errors) == 5


def test_split_stream_title() -> None:
    """The title in the stream splits into artist and title when it can."""
    metadata = split_stream_title("NOORDKAAP - Arme Joe")
    assert metadata is not None
    assert (metadata.artist, metadata.title) == ("NOORDKAAP", "Arme Joe")
    plain = split_stream_title("Willy - ")
    assert plain is not None
    assert (plain.artist, plain.title) == (None, "Willy -")
    assert split_stream_title("  ") is None


# an answer of the search, as JSONP: two groups, the same station in both, and a programme
SEARCH_JSONP = (
    'callback({"responseStatus":"SUCCESS","total":3,"results":{'
    '"1":{"groupID":"DPG","results":{'
    '"1":{"type":"SI","rpId":"5067","serviceName":"Willy","name":"Willy",'
    '"imageUrl":"https://assets.radioplayer.org/056/0565067/600/600/a.png"},'
    '"2":{"type":"OD","rpId":"5067","name":"Willy Class X podcast"}}},'
    '"2":{"groupID":"Other","results":{'
    '"1":{"type":"SI","rpId":"5067","serviceName":"Willy","name":"Willy"},'
    '"2":{"type":"SI","rpId":5010,"serviceName":"","name":"De Tijdloze"},'
    '"3":{"type":"SI","rpId":"x","serviceName":"Broken"}}}}})'
)
SERVICE = {
    "serviceName": "Willy",
    "deleted": False,
    "imageUrl": "https://assets.radioplayer.org/056/0565067/86/48/l45bvtoe.png",
    "streams": [
        {"url": "", "format": "aac"},
        {"url": "https://streams.example/willy/aac?source=radioplayer", "format": "aac"},
        {"url": "https://streams.example/willy/mp3?source=radioplayer", "format": "mp3"},
    ],
}


def test_item_id() -> None:
    """A station id is the country code and the Radioplayer id, with a dash."""
    assert parse_item_id("056-5067") == ("056", "5067")
    assert parse_item_id("56-5067") is None
    assert parse_item_id("056/5067") is None


def test_json_and_jsonp() -> None:
    """Answers come as plain JSON or wrapped in a callback."""
    assert parse_json(' {"a": 1} ') == {"a": 1}
    assert parse_json('callback({"a": [1]});') == {"a": [1]}
    for broken in ("", "callback", "<html>"):
        with pytest.raises(ValueError):  # noqa: PT011 - any parse error will do
            parse_json(broken)


def test_search() -> None:
    """The search gives each station once, without programmes or broken entries."""
    hits = parse_search(parse_json(SEARCH_JSONP), "056")
    assert hits == [
        ("056-5067", "Willy", "https://assets.radioplayer.org/056/0565067/600/600/a.png"),
        ("056-5010", "De Tijdloze", None),
    ]
    assert parse_search({"results": []}, "056") == []
    assert parse_search(None, "056") == []


def test_services() -> None:
    """The directory is grouped by first letter, whatever key it uses, without broken entries."""
    # shaped like the real answer: Belgium has one key for all its stations
    answer = {
        "undefined": [
            {
                "rpId": "5067",
                "serviceName": "Willy",
                "imageUrl": "https://logo.example/w.png",
                "shortDescription": "Hits\nall day",
            },
            {"rpId": "5010", "serviceName": "De Tijdloze", "imageUrl": ""},
            {"rpId": "138", "serviceName": "10 FM"},
            {"rpId": "9", "serviceName": "  Écho "},
            {"rpId": "5067", "serviceName": "Willy again"},
            {"rpId": "x", "serviceName": "No id"},
            {"rpId": "7", "serviceName": ""},
            "junk",
        ]
    }
    assert parse_services(answer, "056") == {
        "D": [("056-5010", "De Tijdloze", None, None)],
        "E": [("056-9", "Écho", None, None)],
        "W": [("056-5067", "Willy", "https://logo.example/w.png", "Hits all day")],
        "#": [("056-138", "10 FM", None, None)],
    }
    assert parse_services(None, "056") == {}


def test_service() -> None:
    """The directory gives the name, the logo and the first stream as it is."""
    assert parse_service(SERVICE) == (
        "Willy",
        SERVICE["imageUrl"],
        "https://streams.example/willy/aac?source=radioplayer",
    )
    assert parse_service({**SERVICE, "streams": []}) == ("Willy", SERVICE["imageUrl"], None)
    assert parse_service({**SERVICE, "deleted": True}) is None
    assert parse_service({"message": "Forbidden"}) is None
    assert parse_service(None) is None


@pytest.mark.parametrize(
    ("timezone", "locale", "countries"),
    [
        # Music Assistant has no nl_BE: a Belgian server has nl_NL or fr_FR
        ("Europe/Brussels", "nl_NL", ["056"]),
        ("Europe/Brussels", "fr_FR", ["056"]),
        ("Europe/Vienna", "de_DE", ["040"]),
        ("America/Toronto", "en_US", ["124"]),
        # a container without a time zone: the language decides
        ("UTC", "en_GB", ["826"]),
        ("UTC", "fr_FR", ["250"]),
        ("UTC", "nl_NL", []),
        ("Asia/Tokyo", "en_US", []),
        ("UTC", "en", []),
    ],
)
def test_default_countries(timezone: str, locale: str, countries: list[str]) -> None:
    """The default country is the one of the server, when Radioplayer runs there."""
    assert default_countries(timezone, locale) == countries
