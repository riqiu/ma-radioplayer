# Music Assistant community provider template

A community provider adds something to [Music Assistant](https://music-assistant.io) from a
repository of its own, with its own documentation, support and releases. An administrator of a
Music Assistant server installs it from the
[community catalog](https://github.com/music-assistant/community-catalog).

This template holds a working music source, `example_music`, that plays a few test tones without
an account or network, with its tests, a development container and CI.

## The rules

A registry maintainer accepts a provider once, for what it does. A provider may do what the
service's own app does for the user's account, reverse engineering included, but never more. The
[community catalog rules](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md)
say exactly what is and is not accepted.

A provider that talks to a music service is a good client of it: a throttler in front of its API,
the cache for what repeats, no new requests while the service asks to wait, and no walking of the
service's catalogue for one user action. The
[developer guide of the server](https://github.com/music-assistant/server/blob/dev/DEVELOPMENT.md)
explains this with the helpers that do it.

## From template to a listed provider

You need [Docker](https://www.docker.com/products/docker-desktop/) and
[VS Code](https://code.visualstudio.com/) with the Dev Containers extension, on Windows, macOS or
Linux.

1. **Use the template.** Click **Use this template**, create your repository and open it in
   VS Code. Choose **Reopen in Container**; the first build downloads the server and takes a few
   minutes.
2. **Name your provider.** Its `domain` is the unique id of the provider in `manifest.json`, such
   as `my_service`; it is not a domain name. It holds lower case letters, digits and underscores,
   and no other provider may use it. In the terminal of VS Code:

   ```sh
   uv run python scripts/rename.py my_service
   uv lock
   ```

   The script renames the folder and sets the domain in the manifest, the tests, the CI and
   `pyproject.toml`. Then delete `scripts/rename.py`; it belongs to the template.
   The provider may also be in a folder named `provider`, or in `providers/my_service` in a
   repository with several providers; set `folder` in `.github/workflows/ci.yml` to that folder.
3. **Write it.** In `manifest.json` and `strings.json`, set the name, the description, your GitHub
   handle in `codeowners` and the address of your documentation. List in `provides` what the
   provider offers whatever its settings or the user's account: media types such as `track` or
   `radio`, or for a plugin its main features such as `audio_source` or `scrobble`. It is for
   discovery only, such as the filters of the Marketplace; what the provider does always comes
   from its code. The
   [developer documentation](https://developers.music-assistant.io/building-a-provider/) explains
   how to build a provider.

   | Path | What it is |
   |---|---|
   | `my_service/__init__.py` | The provider. Its comments show where the real work goes. |
   | `my_service/manifest.json` | Its type, domain, name, code owners and requirements ([all fields](https://github.com/music-assistant/server/blob/dev/DEVELOPMENT.md#%EF%B8%8F-manifest-file)). |
   | `my_service/strings.json` | Its texts in English: the name, the description and the labels of its settings. |
   | `my_service/translations/` | Its texts in other languages the server has, such as `nl.json`. |
   | `my_service/icon.svg` | Its icon. Optional, as are `icon_dark.svg` and `icon_monochrome.svg`. |

   The server's annotated templates (`_demo_music_provider`, `_demo_player_provider` and
   `_demo_plugin_provider` in
   [`music_assistant/providers`](https://github.com/music-assistant/server/tree/dev/music_assistant/providers))
   list every method you can implement. A provider that asks the user for a login or another value
   adds a [setup flow](https://developers.music-assistant.io/setup-flows/). The provider runs inside
   the user's server: it never blocks and never writes into its own folder, and its requirements
   install next to the server's packages, so use the versions the server already has where you can.
4. **Run it.** Run the task **Run Music Assistant** (Terminal > Run Task) or `scripts/run.sh`, or
   press F5 to run it in the debugger. Open <http://localhost:8095>, create the first user, add
   your provider under Settings > Music sources and play a track in the web player; speakers in
   your network usually cannot reach a server inside the container. When the provider is not
   offered there, the output of the server says why; when it fails to start, its entry in the
   settings shows the error. Restart the server after a change to the code; delete `.data/` to
   start over with an empty server. Without the container, on macOS or Linux with
   [uv](https://docs.astral.sh/uv/) and ffmpeg 8 or newer: `uv sync`, then `scripts/run.sh`.
5. **Test it.** `uv run pytest` checks the manifest, the texts, the requirements and the icon, and
   boots a real server that loads the provider and asks it for a track. For your own tests,
   `pytest_plugins = ["music_assistant.testing"]` in `tests/conftest.py` gives the fixture `mass`:
   a started server that loads your repository, announces nothing on the network and listens only
   on the local machine. What your provider asks of its service still goes out. Lint, type check
   and spelling run on every commit once `uv run pre-commit install` has run (the container does
   that). CI runs all of this and the checks the community catalog runs on every release. A check
   that fails says exactly what is wrong;
   [What a provider needs](https://github.com/music-assistant/community-catalog/blob/main/MAINTAINING.md#what-a-provider-needs)
   lists the exact limits.
6. **Publish a release:** a GitHub release, not a pre-release, whose tag is the version, such as
   `v1.2.0`.
7. **Submit it** with the form
   [Submit a provider](https://github.com/music-assistant/community-catalog/issues/new?template=submit-provider.yml).
   It needs a public repository with an open source licence and a release that passes the checks.
   The owner of the repository or a code owner in the manifest of its latest release submits it.
   The result of the checks appears as a comment on the issue; a registry maintainer then decides
   once about what the provider does. The
   [registry's README](https://github.com/music-assistant/community-catalog#admission) has the
   details.

After admission, you release whenever you want: publish a GitHub release, and nothing else is
needed. The registry looks for new releases once a night and offers each version to the channels
it passes on: nightly, beta, and one per stable version such as stable 2.11. When a release fails,
or a version stops loading on a channel, the registry opens an issue on its own repository that
mentions you with the reason, and closes it by itself when a later release passes.
[What the nightly test reports](https://developers.music-assistant.io/building-a-provider/#what-the-nightly-test-reports)
has the details.

## The server version

The server version you develop against is the `music-assistant` line in `pyproject.toml`: a release
tag or a commit of the server after the `@`. To move to another, change that line and run
`uv lock`. Keep the two packages listed under it at the versions in the server's
`music_assistant/providers/sendspin/manifest.json`.

## Licence

Apache-2.0, like Music Assistant. You may choose another open source licence for your provider.
