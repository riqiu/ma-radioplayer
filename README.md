# Radioplayer for Music Assistant

A [Music Assistant](https://music-assistant.io) community provider for radio stations that
show what really plays. It finds the stations of [Radioplayer](https://radioplayer.org) by
search, plays them from the streams Radioplayer itself lists, and asks the public Radioplayer
feed for the artist, the title and the cover of the song on air.

Most radio streams carry a title of their own, but many stations leave the last song in it
during the adverts, the news and the talk, or only send the station name. Radioplayer is the
listings service that many European broadcasters use for their own apps, so its feed follows
the programme.

## What you see

- **During a song:** the artist, the title and the cover from Radioplayer. When the feed only
  has the station logo for that song, Music Assistant looks for a cover itself, as it does for
  other radio stations.
- **Between songs** (adverts, news, talk): the name of the programme or the station, with its
  logo, and no artist.
- **When the feed can't be reached:** the title the stream carries, as for any other radio
  station, until the feed answers again.

## Recently played

Under **Browse > Radioplayer > Recently played**, each station of the settings or of the
library has a folder with the songs it played last, newest first: the artist, the title and
the cover, as far back as the feed goes (about half an hour to an hour). The provider can't
play a single song from the radio, so a song opens the tracks that your other music providers
find for its artist and title, ready to play, followed by the station itself. A song without
an artist in the feed opens the station only. The tracks found are kept for a few minutes and
shared by all users of the server, so on a server with several users one may see tracks
found for another.

## All stations

Under **Browse > Radioplayer > All stations**, the stations of the picked countries are
listed by first letter (names that start with a digit or a symbol are under **#**), and
play or can be added to the library like a station found by search. With several countries
there is a folder per country first. The lists come from the Radioplayer directory of each
country and are kept for a day.

## Install

Install the provider from the
[community catalog](https://github.com/music-assistant/community-catalog) in the settings of
your Music Assistant server, then add it under **Settings > Providers**.

## Find stations

Pick one or more **Countries** in the settings of the provider. Until you pick, it is the
country of your server, from its time zone (`Europe/Brussels` gives Belgium) or else the
language setting of Music Assistant (`en_GB` gives the United Kingdom), when Radioplayer runs
there; otherwise none is picked, and the search answers with one entry that says to pick a
country. The
search of Music Assistant then finds the Radioplayer stations of those countries under
**Radio**; add the ones you want to your library. Radioplayer gives the search on these
countries: Austria, Belgium, Canada, Denmark, Finland, France, Germany, Italy, Luxembourg,
Norway, Spain, Switzerland and the United Kingdom.

A station found by search plays the first stream that Radioplayer lists for it, asked again
at every start. The address is used as given, with the parameters by which the station counts
its listeners.

## Own stations

Optional: the setting **Own stations** is for a station the search doesn't find, such as one
in another country, or one that should play another stream. One station per line, in the form

```text
name | stream URL | Radioplayer id
```

with an optional logo URL at the end:

```text
Willy | https://example.com/willy.mp3 | 056/5067
Studio Brussel | https://example.com/stubru.aac | 056/5001 | https://example.com/stubru.png
```

- **name:** the name to show in Music Assistant.
- **stream URL:** the audio stream of the station, as you would add it as a radio station.
  The provider doesn't look streams up, so use the one you already play.
- **Radioplayer id:** the country code, a slash and the id of the station on Radioplayer (see
  below).
- **logo URL** (optional): the picture of the station in the library.

These stations appear in the library under **Radio**. A line that can't be read is skipped
and named in the log of the server. Two lines with the same Radioplayer id are not allowed, as
the id is the identity of the station in Music Assistant: changing it makes a new station,
and favourites or presets of the old one stop working. A station found by search has the same
id, so a line for it replaces the station's name, stream and logo while favourites and presets
keep working.

### Find the Radioplayer id

The country code is the
[ISO 3166-1 numeric code](https://en.wikipedia.org/wiki/ISO_3166-1_numeric) of the country,
with three digits: `056` for Belgium, `826` for the United Kingdom, `276` for Germany, and so
on. Radioplayer runs in a number of European countries only.

The station id is in the search of the feed. Open, for a station named Willy in Belgium:

```text
https://core-search.radioplayer.cloud/056/qp/v4/search?query=willy
```

and take the `rpId` of the station in the answer, here `5067`, which gives `056/5067`. The
answer also has the `imageUrl` of the logo, which you can use as the logo URL.

To check an id, open the feed of what plays now; it should name the station:

```text
https://core-search.radioplayer.cloud/056/qp/v4/events?rpId=5067
```

## How it works

While a station plays, Music Assistant asks the provider every few seconds what plays. The
provider asks the feed of that station at most as often as the feed says it may (its
`pollNext`, kept between 10 seconds and 5 minutes) and shares the answer between all players
on that station. After a failed request it waits a minute before it asks again. It sends no
account, key or personal data: only the id of the station, or the text searched for.

A search asks each picked country once and keeps the answer for a day. Opening a song under
Recently played searches your music providers for it, as the search of Music Assistant does.
Starting a station found by search asks Radioplayer's station directory for its streams.

The provider tells Music Assistant to keep the title of the stream aside instead of showing
it, and uses that title only while the feed fails.

## Limits

- Only stations on Radioplayer, in a country where it runs, show what plays from the feed.
- The feed, the search and the station directory are the public, unofficial API of the
  Radioplayer web player. They need no key, but have no documentation or promise of
  stability; this project is not affiliated with Radioplayer and may break when they change.
  If the feed changes, the stations still play and show the title of the stream; if the
  directory changes, only own stations play.
- The feed and the audio are not in exact sync: a new song can show some seconds early or
  late.

## Development

The repository follows the
[community provider template](https://github.com/music-assistant/community-provider-template).
With [uv](https://docs.astral.sh/uv/) and Python 3.14:

```shell
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy radioplayer tests
```

The parsing of the settings and of the Radioplayer answers is in `radioplayer/helpers.py`, apart from the
server, with its tests in `tests/test_helpers.py`.

## Licence

[Apache License 2.0](LICENSE). Radioplayer is a trademark of its owners; this project is not
made or endorsed by them.
