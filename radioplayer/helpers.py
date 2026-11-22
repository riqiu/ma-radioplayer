"""Parsing of the station settings and of the Radioplayer answers, without the server."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from music_assistant_models.streamdetails import StreamMetadata

# the countries whose stations can be searched and played: the numeric code that the feed
# and the search use, and the letter code and English name of the station directory that
# lists the streams (Ireland and others are searchable, but their directory is unknown)
COUNTRIES: dict[str, tuple[str, str]] = {
    "040": ("at", "Austria"),
    "056": ("be", "Belgium"),
    "124": ("ca", "Canada"),
    "208": ("dk", "Denmark"),
    "246": ("fi", "Finland"),
    "250": ("fr", "France"),
    "276": ("de", "Germany"),
    "380": ("it", "Italy"),
    "442": ("lu", "Luxembourg"),
    "578": ("no", "Norway"),
    "724": ("es", "Spain"),
    "756": ("ch", "Switzerland"),
    "826": ("uk", "United Kingdom"),
}
# a Radioplayer id: the numeric country code, a slash and the id of the station
RADIOPLAYER_ID = re.compile(r"(\d{3})/(\d+)")
# the id of a station in Music Assistant: the same with a dash
ITEM_ID = re.compile(r"(\d{3})-(\d+)")
# the feed marks a real cover by this path; any other picture is the logo of the station
COVER_PATH = "/coverArt/"
# what the feed says plays stays true this long after its end time, as the stream lags
STALE_AFTER_SECONDS = 30
# the bounds for the pause the feed asks for before the next question (pollNext)
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 300
DEFAULT_POLL_SECONDS = 30


@dataclass(frozen=True)
class Station:
    """A radio station from the settings."""

    item_id: str
    name: str
    stream_url: str
    country: str
    rp_id: str
    logo_url: str | None = None


def parse_stations(lines: list[str]) -> tuple[dict[str, Station], list[str]]:
    """
    Return the stations of the settings by item id, and a reason for each line skipped.

    A line is ``name | stream URL | Radioplayer id`` with an optional ``| logo URL``.

    :param lines: The lines the user entered.
    """
    stations: dict[str, Station] = {}
    errors: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (3, 4):
            errors.append(f"{line!r}: expected name | stream URL | Radioplayer id [| logo URL]")
            continue
        name, stream_url, radioplayer_id, *rest = parts
        logo_url = rest[0] if rest and rest[0] else None
        rp_match = RADIOPLAYER_ID.fullmatch(radioplayer_id)
        if not name:
            errors.append(f"{line!r}: the name is empty")
        elif not _is_web_address(stream_url):
            errors.append(f"{line!r}: the stream URL does not start with http:// or https://")
        elif rp_match is None:
            errors.append(f"{line!r}: the Radioplayer id is not like 056/5067")
        elif logo_url is not None and not _is_web_address(logo_url):
            errors.append(f"{line!r}: the logo URL does not start with http:// or https://")
        else:
            country, rp_id = rp_match.groups()
            item_id = f"{country}-{rp_id}"
            if item_id in stations:
                errors.append(f"{line!r}: Radioplayer id {radioplayer_id} is already used")
                continue
            stations[item_id] = Station(item_id, name, stream_url, country, rp_id, logo_url)
    return stations, errors


def parse_item_id(item_id: str) -> tuple[str, str] | None:
    """
    Return the country code and Radioplayer id of a station id, or None when it is not one.

    :param item_id: The id of a station in Music Assistant, such as 056-5067.
    """
    match = ITEM_ID.fullmatch(item_id)
    return (match[1], match[2]) if match else None


def parse_json(text: str) -> Any:
    """
    Return the content of a JSON answer, or of a JSONP one such as ``callback({...})``.

    :param text: The answer as text; ValueError when it is neither.
    """
    text = text.strip()
    if not text.startswith(("{", "[")):
        start, end = text.find("("), text.rfind(")")
        if start < 0 or end < start:
            raise ValueError("the answer is neither JSON nor JSONP")
        text = text[start + 1 : end]
    return json.loads(text)


def parse_search(answer: Any, country: str) -> list[tuple[str, str, str | None]]:
    """
    Return the stations in an answer of the search: item id, name and logo, each station once.

    :param answer: The answer of the search, whose results come in groups.
    :param country: The numeric code of the country searched.
    """
    found: dict[str, tuple[str, str, str | None]] = {}
    groups = answer.get("results") if isinstance(answer, dict) else None
    for group in _values(groups):
        for item in _values(group.get("results") if isinstance(group, dict) else None):
            # the search also finds programmes to listen to again (type OD)
            if not isinstance(item, dict) or item.get("type") != "SI":
                continue
            rp_id = str(item.get("rpId") or "").strip()
            name = _text(item.get("serviceName")) or _text(item.get("name"))
            if rp_id.isdigit() and name:
                item_id = f"{country}-{rp_id}"
                found.setdefault(item_id, (item_id, name, _text(item.get("imageUrl")) or None))
    return list(found.values())


def parse_service(service: Any) -> tuple[str, str | None, str | None] | None:
    """
    Return the name, logo and first stream of a station in the directory.

    None when the answer names no station or the station is deleted; the stream is None
    when the station lists none. The streams are kept as given, with the parameters by which
    the station counts its listeners.

    :param service: The answer of the directory for one station.
    """
    if not isinstance(service, dict) or service.get("deleted") is True:
        return None
    name = _text(service.get("serviceName"))
    if not name:
        return None
    streams = [_text(s.get("url")) for s in _values(service.get("streams")) if isinstance(s, dict)]
    stream = next((url for url in streams if _is_web_address(url)), None)
    return name, _text(service.get("imageUrl")) or None, stream


def parse_now_playing(events: dict[str, Any], now: float) -> StreamMetadata | None:
    """
    Return what plays now according to an answer of the events feed.

    A song has its artist, title and cover; the image is None when the feed has only the
    logo of the station, so the server can look for a cover. Between songs the result has
    the name of the programme or station and the logo, without an artist. None when the
    feed says nothing.

    :param events: The answer of the events feed.
    :param now: The current time, in seconds since the epoch.
    """
    results = events.get("results")
    current = results.get("now") if isinstance(results, dict) else None
    if not isinstance(current, dict):
        return None
    title = _text(current.get("name"))
    artist = _text(current.get("artistName"))
    image = _text(current.get("imageUrl")) or None
    stop = _epoch(current.get("stopTime"))
    is_song = current.get("song") is True and bool(title)
    if is_song and (stop is None or now <= stop + STALE_AFTER_SECONDS):
        cover = image if image and COVER_PATH in image else None
        return StreamMetadata(title=title, artist=artist or None, image_url=cover)
    station_title = _text(current.get("serviceName"))
    if is_song or not (title or station_title):
        # an ended song still names its cover, never the logo, so it shows nothing
        return None
    logo = image if image and COVER_PATH not in image else None
    return StreamMetadata(title=title or station_title, image_url=logo)


def poll_seconds(events: dict[str, Any]) -> float:
    """
    Return how long to wait before asking the feed again, within sane bounds.

    :param events: The answer of the events feed, whose pollNext is in milliseconds.
    """
    poll_next = events.get("pollNext")
    if isinstance(poll_next, bool) or not isinstance(poll_next, int | float):
        return DEFAULT_POLL_SECONDS
    return min(max(poll_next / 1000, MIN_POLL_SECONDS), MAX_POLL_SECONDS)


def split_stream_title(stream_title: str) -> StreamMetadata | None:
    """
    Return the metadata of an ``Artist - Title`` stream title, or None when it is empty.

    :param stream_title: The title the stream carries.
    """
    stream_title = stream_title.strip()
    if not stream_title:
        return None
    artist, separator, title = stream_title.partition(" - ")
    if not separator or not title.strip():
        return StreamMetadata(title=stream_title)
    return StreamMetadata(title=title.strip(), artist=artist.strip() or None)


def _is_web_address(value: str) -> bool:
    """Return whether a value is an http or https address."""
    return value.startswith(("http://", "https://")) and len(value) > len("https://")


def _values(value: Any) -> Iterable[Any]:
    """Return the items of a list, or the values of a dict; nothing for anything else."""
    if isinstance(value, dict):
        return value.values()
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    """Return a feed value as stripped text, or an empty text when it is none."""
    return value.strip() if isinstance(value, str) else ""


def _epoch(value: Any) -> float | None:
    """Return a feed time (epoch seconds, as text) as a number, or None when it is none."""
    try:
        return float(value) if value not in (None, "") else None
    except TypeError, ValueError:
        return None
