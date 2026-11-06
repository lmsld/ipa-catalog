# IPA Catalog

A self-hosted web app that publishes a SideStore/AltStore-compatible JSON source
from direct IPA URLs and public GitHub releases. The public home page lists apps
and lets visitors choose their preferred store from one Add to button; catalog
management lives at `/admin`.

## Run with Podman

```sh
cd ipa-catalog
cp .env.example .env
# Set ADMIN_TOKEN in .env to a long, unique secret.
podman compose up --build -d
```

`podman compose` requires an external Compose provider such as `podman-compose`
or `docker-compose`. To prefer `podman-compose` when both providers are
installed, set `PODMAN_COMPOSE_PROVIDER=podman-compose` or configure
`compose_providers` in `containers.conf`.

By default, Compose publishes the service on `127.0.0.1` only. Open
`http://localhost:8000` for the public catalog and
`http://localhost:8000/admin` to manage it. The admin page prompts for
`ADMIN_TOKEN`; management API requests use that token as a bearer token. The
token is kept only in the browser's session storage. The admin page itself is
publicly reachable, but management data and actions are protected by the token.
For access from outside the host, put the service behind an HTTPS reverse proxy.
If the proxy runs on another host, set `BIND_ADDRESS=0.0.0.0` to expose the
service on host interfaces, and restrict access with firewall rules and HTTPS.
Use the Repository details form on `/admin` to set the source name, identifier,
public description, Add to button label, and available store options (display
name and URL scheme). These values are persisted in the YAML catalog; the
matching `.env` values are only initial defaults for source details.
Admin sections collapse to keep the page compact, and app cards expand when
you need to edit their details.

Import `http://localhost:8000/source.json` into SideStore, AltStore, FlareStore,
or another compatible client. For access outside the host, place the service
behind HTTPS and set `PUBLIC_BASE_URL` to the public base URL (without a
trailing slash).

## Adding and refreshing apps

From `/admin`, add a direct HTTP(S) URL to an IPA or a GitHub repository URL
such as `https://github.com/owner/repo`. A GitHub listing is checked by querying
the repository's latest-release API at that app's configured polling interval.
The default interval is 24 hours; an interval of 1 hour is available. The
scheduler wakes every 30 seconds to discover entries that are due, then checks
the latest release, downloads the corresponding IPA asset, and reads its actual
bundle metadata. It does not poll the IPA download URL alone, because that
would not reveal a newly published GitHub release.

For a repository, the app finds every `.ipa` asset in the latest published
release and adds each asset as a catalog listing. Each listing tracks the IPA
at the same position in later releases, so release asset ordering should remain
stable. Assets with the same bundle identifier are grouped into a single source
app with multiple version download entries. A release without IPA assets is
reported as an error.

The developer name is optional: the app reads it from recognized developer
fields in `Info.plist` or `iTunesMetadata.plist` when available. It also
extracts bundle identifier, version, display name, minimum iOS version, file
size, and an icon when a PNG/JPEG icon is present in the app bundle.
Descriptions are not reliably embedded in IPAs, so they can be filled in or
changed in the admin page.

Each app can use a polling interval from 1 to 720 hours, or can be checked
manually. When a new version is found, the new version is placed first in the
source feed's `versions` array; prior versions remain available in history. If
a URL fails or no longer points to a compatible IPA, the error is shown in the
management UI and the next check is rescheduled.

The server accepts public HTTP(S) IPA URLs only, rejects URLs with credentials,
limits IPA downloads to 512 MiB, and checks redirected destinations as well.
The source feed and app icons are public so compatible clients can fetch them
without authentication.

## Persistence

Catalog state, including IPA/GitHub links, metadata, icons, and version history,
along with repository name, identifier, and description, is stored in
`/data/catalog.yaml` within the `ipa-catalog-data` named volume. The YAML file
is written atomically after catalog changes and can be backed up or edited
while the service is stopped.

## Configuration

| Variable | Description |
| --- | --- |
| `SOURCE_NAME` | Initial public source name; editable in `/admin` |
| `SOURCE_IDENTIFIER` | Initial public source identifier; editable in `/admin` |
| `SOURCE_DESCRIPTION` | Initial public source description; editable in `/admin` |
| `ADMIN_TOKEN` | Bearer token required for all management API endpoints |
| `PUBLIC_BASE_URL` | Optional external base URL used for source and icon links |
| `PORT` | Host port to publish (default `8000`) |
| `BIND_ADDRESS` | Host interface to publish on (default `127.0.0.1`; use `0.0.0.0` only when protected by a trusted proxy/firewall) |

## Development

```sh
cd ipa-catalog
python -m pip install -r requirements.txt
ADMIN_TOKEN=dev-token DATA_FILE_PATH=./data/catalog.yaml python -m app.server
python -m unittest discover -s tests
```

The container entrypoint handles `SIGINT` and `SIGTERM` as graceful shutdown
requests, allowing the current request and background scheduler to stop cleanly.
The Compose configuration allows up to 40 seconds for shutdown before
force-stopping the container.
