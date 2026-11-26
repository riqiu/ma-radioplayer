"""Tests of the provider, loaded by a real server."""

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from music_assistant_models.enums import MediaType, ProviderTier, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import BrowseFolder, Radio

from music_assistant.controllers.streams.constants import (
    STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY,
    STREAMDETAILS_INBAND_TITLE_KEY,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.music_provider import MusicProvider
from tests import DOMAIN, live

STATIONS = [
    "Willy | https://stream.example/willy.mp3 | 056/5067",
    "Studio Brussel | https://stream.example/stubru.aac | 056/5001 | https://img.example/s.png",
]
COVER = "https://assets.radioplayer.org/coverArt/96cd3aeaeb3c9b62f0c016f9cfa6c58d.jpg"
BIG = "https://assets.radioplayer.org/056/0565067/600/600/a.png"
SMALL = "https://assets.radioplayer.org/056/0565010/86/48/b.png"


async def _provider(mass: MusicAssistant, countries: list[str] | None = None) -> MusicProvider:
    """Add the provider and give it the test stations; no country, so no search goes out."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    instance_id = step.result["instance_id"]
    values: dict[str, Any] = {"stations": STATIONS, "countries": countries or []}
    config = await mass.config.save_provider_config(DOMAIN, values, instance_id)
    # the server reloads a provider a second after its settings change; do it now
    await mass.load_provider_config(config)
    provider = mass.get_provider(instance_id)
    assert isinstance(provider, MusicProvider)
    return provider


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_stations(mass: MusicAssistant) -> None:
    """The stations of the settings are in the library, found by search and streamed."""
    provider = await _provider(mass)
    radios = [radio async for radio in provider.get_library_radios()]
    assert [radio.name for radio in radios] == ["Willy", "Studio Brussel"]
    assert radios[1].image is not None

    result = await provider.search("brussel", [MediaType.RADIO])
    assert [radio.name for radio in result.radio] == ["Studio Brussel"]
    radio = result.radio[0]
    assert isinstance(radio, Radio)

    # no country picked and no station found: one entry says how to find stations
    hints = (await provider.search("nothing", [MediaType.RADIO])).radio
    assert [hint.name for hint in hints] == [
        "Pick one or more countries in the Radioplayer settings"
    ]
    assert isinstance(hints[0], Radio)
    assert not any(mapping.available for mapping in hints[0].provider_mappings)
    with pytest.raises(MediaNotFoundError, match="Pick one or more countries"):
        await provider.get_stream_details(hints[0].item_id, MediaType.RADIO)

    details = await provider.get_stream_details(radio.item_id, MediaType.RADIO)
    assert details.stream_type == StreamType.HTTP
    assert details.path == "https://stream.example/stubru.aac"
    assert details.data[STREAMDETAILS_INBAND_TITLE_HANDOFF_KEY] is True
    assert details.stream_metadata_update_callback is not None


async def test_now_playing(mass: MusicAssistant) -> None:
    """What plays comes from the feed, and from the stream only while the feed fails."""
    provider = await _provider(mass)
    details = await provider.get_stream_details("056-5067", MediaType.RADIO)
    callback = details.stream_metadata_update_callback
    assert callback is not None
    answers: list[dict[str, Any] | None] = [
        {
            "results": {
                "now": {
                    "name": "Arme Joe",
                    "artistName": "NOORDKAAP",
                    "song": True,
                    "imageUrl": COVER,
                    "stopTime": "",
                }
            },
            "pollNext": 6000,
        },
        None,
    ]

    async def events(*_ids: str) -> dict[str, Any] | None:
        return answers.pop(0)

    looked_up: list[str] = []

    async def artwork(streamdetails: Any) -> None:
        looked_up.append(streamdetails.stream_metadata.title)

    # no network in tests: the feed and the server's cover search are stand-ins
    provider._events = events  # type: ignore[attr-defined]  # noqa: SLF001
    mass.metadata.update_radio_stream_artwork = artwork  # type: ignore[method-assign]

    # the stream still names the song before: the feed decides
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Old Artist - Old Song"
    await callback(details, 0)
    assert details.stream_metadata is not None
    assert (details.stream_metadata.artist, details.stream_metadata.title) == (
        "NOORDKAAP",
        "Arme Joe",
    )
    assert details.stream_metadata.image_url == COVER

    # the feed fails: the stream's own title is shown
    details.data[STREAMDETAILS_INBAND_TITLE_KEY] = "Next Artist - Next Song"
    await callback(details, 5)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Next Song"
    # a song without a cover from the feed asks the server for one, once
    assert looked_up == ["Next Song"]


async def test_recently_played(mass: MusicAssistant) -> None:
    """Browse has the stations and a folder per station with the songs it played last."""
    provider = await _provider(mass)
    feed = {
        "results": {
            "previous": [
                {"name": "Old Song", "artistName": "OLD", "song": True, "startTime": "1"},
                {"name": "Arme Joe", "artistName": "NOORDKAAP", "song": True, "imageUrl": COVER},
            ]
        }
    }
    asked: list[tuple[str, ...]] = []

    async def events(*ids: str) -> dict[str, Any]:
        asked.append(ids)
        return feed

    # no network in tests: the feed is a stand-in
    provider._events = events  # type: ignore[attr-defined]  # noqa: SLF001

    root = await provider.browse(f"{provider.instance_id}://")
    # no country picked: All stations still lists every country
    names = [item.name for item in root]
    assert names == ["Willy", "Studio Brussel", "Recently played", "All stations"]
    recent = root[-2]
    assert isinstance(recent, BrowseFolder)

    stations = await provider.browse(recent.path)
    assert [item.name for item in stations] == ["Willy", "Studio Brussel"]
    willy = stations[0]
    assert isinstance(willy, BrowseFolder)
    assert willy.path == f"{provider.instance_id}://recent/056-5067"

    songs = await provider.browse(willy.path)
    assert [song.name for song in songs] == ["NOORDKAAP - Arme Joe", "OLD - Old Song"]
    assert all(isinstance(song, BrowseFolder) and not song.is_playable for song in songs)
    assert songs[0].image is not None
    assert songs[0].image.path == COVER
    assert songs[1].item_id == "056-5067-1"
    assert asked == [("056", "5067")]
    assert await provider.browse(f"{provider.instance_id}://recent/bad") == []

    searched: list[tuple[str, list[MediaType], int]] = []

    async def search(query: str, media_types: list[MediaType], limit: int) -> Any:
        searched.append((query, media_types, limit))
        return SimpleNamespace(tracks=["the track"])

    # no music providers in tests: the server's search is a stand-in
    mass.music.search = search  # type: ignore[assignment,method-assign]

    newest, oldest = [song for song in songs if isinstance(song, BrowseFolder)]
    # a song opens what the music providers find for it, then the station
    assert oldest.path == f"{willy.path}/1"
    found = await provider.browse(oldest.path)
    assert searched == [("OLD Old Song", [MediaType.TRACK], 5)]
    assert found[0] == "the track"
    assert isinstance(found[1], Radio)
    assert found[1].item_id == "056-5067"
    # a song without a start time opens as well, and never the list it is in
    assert newest.path.rsplit("/", 1)[1].startswith("x")
    found = await provider.browse(newest.path)
    assert searched[-1] == ("NOORDKAAP Arme Joe", [MediaType.TRACK], 5)
    assert found[0] == "the track"
    # a song gone from the feed still offers the station
    gone = await provider.browse(f"{willy.path}/999")
    assert [item.name for item in gone] == ["Willy"]
    assert len(searched) == 2


def _folders(items: Iterable[Any]) -> list[BrowseFolder]:
    """Return the folders of a browsed level."""
    return [item for item in items if isinstance(item, BrowseFolder)]


async def test_all_stations(mass: MusicAssistant) -> None:
    """All stations lists every country, the picked one first, then the letters of a country."""
    services = [
        live("5067", "Willy", description="Hits", multimedia=[{"url": BIG}]),
        live("5010", "De Tijdloze"),
        live("138", "10 FM"),
    ]

    async def fetch(_url: str, _params: dict[str, str] | None = None) -> Any:
        return services

    one = await _provider(mass, ["056"])
    one._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001
    root = await one.browse(f"{one.instance_id}://")
    assert root[-1].name == "All stations"
    countries = _folders(await one.browse(_folders(root)[-1].path))
    assert [c.name for c in countries[:3]] == ["Belgium", "Austria", "Canada"]
    assert len(countries) == 13
    base = f"{one.instance_id}://all/056"
    letters = _folders(await one.browse(countries[0].path))
    assert [item.name for item in letters] == ["D", "W", "#"]
    assert [item.path for item in letters][-1] == f"{base}/other"
    stations = await one.browse(letters[1].path)
    assert [(s.item_id, s.name) for s in stations] == [("056-5067", "Willy")]
    assert isinstance(stations[0], Radio)
    # Willy is in the settings: it keeps its own logo (none), not the directory's
    assert stations[0].image is None
    assert stations[0].metadata.description == "Hits"
    assert [item.item_id for item in letters] == ["056/D", "056/W", "056/other"]
    other = await one.browse(letters[-1].path)
    assert [s.item_id for s in other] == ["056-138"]
    assert await one.browse(f"{base}/Q") == []
    # a lowercase letter works; a country unknown, or a second level of a calm letter, does not
    assert [s.item_id for s in await one.browse(f"{base}/w")] == ["056-5067"]
    assert await one.browse(f"{one.instance_id}://all/999") == []
    assert await one.browse(f"{one.instance_id}://all/W") == []
    assert await one.browse(f"{base}/W/WI") == []
    # a country that is not picked lists its stations as well
    assert [s.item_id for s in await one.browse(f"{one.instance_id}://all/276/w")] == ["276-5067"]


async def test_all_stations_languages(mass: MusicAssistant) -> None:
    """A country with stations in several languages has a folder per language, most used first."""
    services = [
        live("5067", "Willy", "nl"),
        live("5010", "De Tijdloze", "nl"),
        live("6000", "Bel RTL", "fr"),
        live("7000", "Radio Xyz", "xx"),
    ]

    async def fetch(_url: str, _params: dict[str, str] | None = None) -> Any:
        return services

    provider = await _provider(mass, ["056"])
    provider._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001
    base = f"{provider.instance_id}://all/056"
    languages = _folders(await provider.browse(base))
    assert [(f.item_id, f.name) for f in languages] == [
        ("056/nl", "Dutch"),
        ("056/fr", "French"),
        ("056/xx", "xx"),
    ]
    letters = _folders(await provider.browse(languages[0].path))
    assert [(f.item_id, f.name) for f in letters] == [("056/nl/D", "D"), ("056/nl/W", "W")]
    assert [s.item_id for s in await provider.browse(letters[1].path)] == ["056-5067"]
    assert [s.item_id for s in await provider.browse(f"{base}/fr/b")] == ["056-6000"]
    # a letter of another language, or a language the country lacks, is empty
    assert await provider.browse(f"{base}/fr/W") == []
    assert await provider.browse(f"{base}/de") == []
    assert await provider.browse(f"{base}/W") == []


async def test_all_stations_crowded(mass: MusicAssistant) -> None:
    """A folder with more than 150 stations splits by longer prefixes until none is crowded."""
    crowded = [live(str(1000 + n), f"Radio {n}") for n in range(1500)]
    crowded += [live("1", "Rádio X"), live("2", "Øst")]
    few = [live(str(5000 + n), f"Test {n}") for n in range(150)]

    async def fetch(_url: str, _params: dict[str, str] | None = None) -> Any:
        return crowded + few

    provider = await _provider(mass, ["056", "276"])
    provider._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001
    base = f"{provider.instance_id}://all/056"
    letters = _folders(await provider.browse(base))
    assert [item.name for item in letters] == ["R", "T", "Ø"]
    assert letters[0].item_id == "056/R"
    # Radio... has one child at RA, RAD, ...: the folders start where the names differ
    second = _folders(await provider.browse(letters[0].path))
    assert [item.name for item in second] == [f"RADIO_{c}" for c in "0123456789X"]
    assert second[0].path == f"{base}/R/RADIO_0"

    sizes: list[int] = []
    depths: list[int] = []

    async def walk(path: str, depth: int) -> None:
        items = await provider.browse(path)
        for folder in _folders(items):
            await walk(folder.path, depth + 1)
        if radios := [item for item in items if not isinstance(item, BrowseFolder)]:
            sizes.append(len(radios))
            depths.append(depth)

    await walk(letters[0].path, 1)
    assert sum(sizes) == 1501
    assert max(sizes) <= 150
    assert max(depths) == 3
    # 150 stations stay flat, and a folder that does not exist is empty
    assert len(await provider.browse(letters[1].path)) == 150
    assert await provider.browse(f"{base}/R/ZZ") == []


async def test_all_stations_crowded_language(mass: MusicAssistant) -> None:
    """Under a language folder a crowded letter splits the same way."""
    crowded = [live(str(1000 + n), f"Radio {n}", "nl") for n in range(1500)]
    few = [live(str(5000 + n), f"Test {n}", "fr") for n in range(150)]

    async def fetch(_url: str, _params: dict[str, str] | None = None) -> Any:
        return crowded + few

    provider = await _provider(mass, ["056"])
    provider._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001
    base = f"{provider.instance_id}://all/056"
    assert [f.item_id for f in _folders(await provider.browse(base))] == ["056/nl", "056/fr"]
    second = _folders(await provider.browse(f"{base}/nl/R"))
    assert second[0].path == f"{base}/nl/R/RADIO_0"
    assert second[0].item_id == "056/nl/R/RADIO_0"

    async def count(path: str) -> int:
        items = await provider.browse(path)
        radios = [item for item in items if not isinstance(item, BrowseFolder)]
        assert len(radios) <= 150
        return len(radios) + sum([await count(f.path) for f in _folders(items)])

    # every Radio station of Dutch is reached once, the French ones stay apart
    assert await count(f"{base}/nl/R") == 1500
    assert len(await provider.browse(f"{base}/fr/T")) == 150


async def test_all_stations_unreachable(mass: MusicAssistant) -> None:
    """When the directory can't be reached the folder is empty and the root still works."""

    async def fetch(_url: str, _params: dict[str, str] | None = None) -> Any:
        raise ProviderUnavailableError("down")

    provider = await _provider(mass, ["056"])
    provider._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001
    assert await provider.browse(f"{provider.instance_id}://all/056") == []
    assert len(await provider.browse(f"{provider.instance_id}://")) == 4

    # an error that came with a 200 is no list, and is not kept
    junks: list[Any] = [{"error": "busy"}, {}, [], ["x"], "x", None]
    for junk in junks:

        async def answer(_url: str, _params: dict[str, str] | None = None, junk: Any = junk) -> Any:
            return junk

        provider._fetch = answer  # type: ignore[attr-defined]  # noqa: SLF001
        assert await provider._live_stations("056") is None  # type: ignore[attr-defined]  # noqa: SLF001


async def test_all_stations_countries(mass: MusicAssistant) -> None:
    """The picked countries come first, in the order of the settings, then the rest by name."""

    async def fetch(_url: str, _params: dict[str, str] | None = None) -> Any:
        return [live("5010", "De Tijdloze")]

    several = await _provider(mass, ["276", "056"])
    several._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001
    countries = _folders(await several.browse(f"{several.instance_id}://all"))
    assert [(c.item_id, c.name) for c in countries[:3]] == [
        ("276", "Germany"),
        ("056", "Belgium"),
        ("040", "Austria"),
    ]
    assert countries[-1].name == "United Kingdom"
    letters = _folders(await several.browse(countries[0].path))
    assert letters[0].path == f"{several.instance_id}://all/276/D"
    stations = await several.browse(letters[0].path)
    assert [s.item_id for s in stations] == ["276-5010"]
    assert await several.browse(f"{several.instance_id}://all/999") == []


async def test_default_country(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The countries default to the one of the server: its time zone, else its language."""
    provider = await _provider(mass)

    async def default() -> Any:
        entries = await provider.get_config_entries()
        return next(e for e in entries if e.key == "countries").default_value

    # a Belgian browser: Music Assistant keeps nl_NL, the time zone still says Belgium
    mass.metadata.set_preferred_language("nl-BE")
    monkeypatch.setenv("TZ", "Europe/Brussels")
    assert await default() == ["056"]
    monkeypatch.setenv("TZ", "UTC")
    assert await default() == []
    mass.metadata.set_preferred_language("en-GB")
    assert await default() == ["826"]


async def test_radioplayer_stations(mass: MusicAssistant) -> None:
    """Search finds Radioplayer's stations, which play the stream its live stations list."""
    provider = await _provider(mass, ["056"])
    search = {
        "results": {
            "1": {
                "results": {
                    "1": {"type": "SI", "rpId": "5067", "serviceName": "Willy", "imageUrl": BIG},
                    "2": {"type": "SI", "rpId": "5010", "serviceName": "De Tijdloze"},
                }
            }
        }
    }
    station = live("5010", "De Tijdloze", multimedia=[{"url": BIG}])
    # a station the live stations lack is still in the old directory
    service = {
        "serviceName": "Mökkiradio",
        "imageUrl": SMALL,
        "streams": [{"url": "https://streams.example/mokki.mp3?rp_source=1"}],
    }
    posted: list[Any] = []

    async def fetch(url: str, _params: dict[str, str] | None = None, body: Any = None) -> Any:
        if "/search" in url:
            return search
        if url.endswith("/api/live-stations"):
            posted.append(body)
            return [station] if body == {"requestMap": {"056": ["5010"]}} else []
        return service if url.endswith("/be/public/service/226") else None

    # no network in tests: the answers of Radioplayer are stand-ins
    provider._fetch = fetch  # type: ignore[attr-defined]  # noqa: SLF001

    result = await provider.search("w", [MediaType.RADIO], limit=10)
    # Willy is in the settings: it keeps its own entry and is not doubled
    assert [radio.item_id for radio in result.radio] == ["056-5067", "056-5010"]
    assert result.radio[0].image is None

    radio = await provider.get_radio("056-5010")
    assert radio.name == "De Tijdloze"
    assert radio.image is not None
    assert radio.image.path == BIG

    details = await provider.get_stream_details("056-5010", MediaType.RADIO)
    assert details.path == "https://streams.example/5010.mp3"
    assert details.stream_metadata_update_callback is not None
    assert posted[-1] == {"requestMap": {"056": ["5010"]}}

    radio = await provider.get_radio("056-226")
    assert (radio.name, radio.image.path if radio.image else None) == ("Mökkiradio", SMALL)
    details = await provider.get_stream_details("056-226", MediaType.RADIO)
    assert details.path == "https://streams.example/mokki.mp3?rp_source=1"

    with pytest.raises(MediaNotFoundError):
        await provider.get_radio("056-1")
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("999-1", MediaType.RADIO)
    station["liveStreams"] = []
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("056-5010", MediaType.RADIO)

    async def unreachable(_url: str, _params: dict[str, str] | None = None, **_: Any) -> Any:
        raise ProviderUnavailableError("down")

    # Radioplayer down: the station is not called gone, and a search still answers
    provider._fetch = unreachable  # type: ignore[attr-defined]  # noqa: SLF001
    with pytest.raises(ProviderUnavailableError):
        await provider.get_radio("056-5010")
    result = await provider.search("other", [MediaType.RADIO])
    assert result.radio == []


async def test_working_stream(mass: MusicAssistant, aiohttp_server: Any) -> None:
    """Of several streams the first that answers plays; a single one is not checked."""
    asked: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        asked.append(request.path)
        if request.path.startswith("/gone"):
            raise web.HTTPNotFound
        return web.Response(body=b"audio", content_type="audio/mpeg")

    app = web.Application()
    app.router.add_get("/{name}", handle)
    server = await aiohttp_server(app)
    provider = await _provider(mass, ["246"])
    pick = provider._working_stream  # type: ignore[attr-defined]  # noqa: SLF001

    gone, works = str(server.make_url("/gone.aac")), str(server.make_url("/works.mp3"))
    assert await pick([gone, works, str(server.make_url("/later.mp3"))]) == works
    assert asked == ["/gone.aac", "/works.mp3"]
    asked.clear()
    assert await pick([gone]) == gone
    assert asked == []
    # none answers: the first is played, and the server reports why it fails
    assert await pick([gone, str(server.make_url("/gone2"))]) == gone
