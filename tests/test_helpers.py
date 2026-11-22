"""Tests of the parsing of the settings and of the feed, with answers shaped like the real ones."""

from typing import Any

from radioplayer.helpers import (
    DEFAULT_POLL_SECONDS,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    STALE_AFTER_SECONDS,
    parse_now_playing,
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
