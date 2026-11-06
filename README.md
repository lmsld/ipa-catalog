# IPA Catalog

A self-hosted web app that publishes a SideStore/AltStore-compatible JSON source
from direct IPA URLs and public GitHub releases.
By default, the landing page lists all the ipa added/found.
Admin page is at /admin and is protecte with an env `ADMIN_TOKEN` secret.

## Build instruction

```sh
git clone https://github.com/lmsld/ipa-catalog.git
cd ipa-catalog
podman build -t ipa-catalog .
```

## Run with Podman

```sh
podman run -e ADMIN_TOKEN=never-gonna-give-you-up \
-v ./data:/data -p 8000:8000 ipa-catalog
```

## Run with docker compose
```
wget https://raw.githubusercontent.com/lmsld/ipa-catalog/refs/heads/main/docker-compose.yml
echo "ADMIN_TOKEN=$(openssl rand -base64 32)" > .env
docker compose up -d
```

## Additional information

`./data/catalog.yaml` is used to keep a record of repo and ipa.