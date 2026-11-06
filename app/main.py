"""IPA Catalog API and web application."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import os
import secrets
import struct
import zlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .ipa import IPAError, IPAMetadata, inspect_ipa
from .github import GitHubReleaseError, latest_ipa_assets, parse_repository_url
from .state import CatalogStore

APP_DIR = Path(__file__).parent
DATA_FILE_PATH = os.environ.get("DATA_FILE_PATH", "./data/catalog.yaml")
store = CatalogStore(DATA_FILE_PATH)
DEFAULT_INTERVAL_HOURS = 24
MAX_INTERVAL_HOURS = 24 * 30


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _default_settings() -> dict[str, str]:
    return {
        "name": os.environ.get("SOURCE_NAME", "My IPA Catalog"),
        "identifier": os.environ.get("SOURCE_IDENTIFIER", "com.example.ipacatalog"),
        "description": os.environ.get("SOURCE_DESCRIPTION", ""),
        "add_button_label": "Add to…",
        "store_options": [
            {"name": "SideStore", "scheme": "sidestore"},
            {"name": "FlareStore", "scheme": "flarestore"},
            {"name": "AltStore", "scheme": "altstore"},
        ],
    }


def _settings() -> dict[str, Any]:
    defaults = _default_settings()
    source_settings = store.read().get("source", {})
    if not isinstance(source_settings, dict):
        source_settings = {}
    return {
        key: source_settings.get(key, value)
        for key, value in defaults.items()
    }


def _app_to_dict(app: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in app.items() if key != "icon"}


def _get_app(app_id: str) -> dict[str, Any]:
    app = next((item for item in store.read()["apps"] if item["id"] == app_id), None)
    if app is None:
        raise HTTPException(status_code=404, detail="App not found.")
    return app


def _public_source(request: Request) -> dict[str, Any]:
    rows = sorted(store.read()["apps"], key=lambda app: app["name"].casefold())
    base_url = _public_base_url(request)
    settings = _settings()
    grouped_apps: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped_apps.setdefault(row["bundle_identifier"], []).append(row)

    apps = []
    for bundle_identifier, app_rows in grouped_apps.items():
        primary = app_rows[0]
        app_versions = []
        for row in app_rows:
            app_versions.extend(row["versions"])
        app_versions.sort(
            key=lambda version: (
                version.get("date", ""),
                tuple(
                    int(part)
                    for part in version.get("minOSVersion", "").split(".")
                    if part.isdigit()
                ),
            ),
            reverse=True,
        )
        subtitles = list(dict.fromkeys(row["subtitle"] for row in app_rows if row["subtitle"]))
        subtitle = primary["subtitle"]
        if len(subtitles) > 1 and all(row["source_type"] == "github" for row in app_rows):
            subtitle = "Variants: " + ", ".join(subtitles)

        icon_url = primary["icon_url_override"] or f"{base_url}/icons/{primary['id']}"
        if not app_versions:
            continue
        app: dict[str, Any] = {
            "name": primary["name"],
            "bundleIdentifier": bundle_identifier,
            "developerName": next(
                (row["developer_name"] for row in app_rows if row["developer_name"]),
                "",
            ),
            "localizedDescription": primary["description"],
            "iconURL": icon_url,
            "versions": app_versions,
        }
        if subtitle:
            app["subtitle"] = subtitle
        apps.append(app)
    return {
        "name": settings["name"],
        "identifier": settings["identifier"],
        "sourceURL": f"{base_url}/source.json",
        "apps": apps,
    }


def _public_base_url(request: Request) -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")


def _save_error(app_id: str, error: Exception) -> None:
    now = _now()
    with store.edit() as state:
        row = next((app for app in state["apps"] if app["id"] == app_id), None)
        if row is not None:
            row["last_checked"] = _iso(now)
            row["next_check"] = _iso(now + timedelta(hours=row["poll_interval_hours"]))
            row["last_error"] = str(error)


def _apply_metadata(
    app_id: str,
    metadata: IPAMetadata,
    *,
    source_url: str | None = None,
    release_tag: str = "",
    release_asset_name: str = "",
    release_date: str = "",
) -> dict[str, Any]:
    now = _now()
    with store.edit() as state:
        row = next((app for app in state["apps"] if app["id"] == app_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="App not found.")
        if metadata.bundle_identifier != row["bundle_identifier"]:
            raise IPAError(
                "The IPA's bundle identifier changed; refusing to replace a different app."
            )
        versions = row["versions"]
        current_download_url = source_url or row["source_url"]
        if metadata.version != row["version"]:
            version: dict[str, Any] = {
                "version": metadata.version,
                "date": release_date or now.date().isoformat(),
                "downloadURL": current_download_url,
                "size": metadata.size,
            }
            if metadata.minimum_os_version:
                version["minOSVersion"] = metadata.minimum_os_version
            if row["version_description"]:
                version["localizedDescription"] = row["version_description"]
            versions.insert(0, version)
        else:
            if versions:
                versions[0]["downloadURL"] = current_download_url
                versions[0]["size"] = metadata.size
                if metadata.minimum_os_version:
                    versions[0]["minOSVersion"] = metadata.minimum_os_version
                if row["version_description"]:
                    versions[0]["localizedDescription"] = row["version_description"]
                else:
                    versions[0].pop("localizedDescription", None)
            else:
                version = {
                    "version": metadata.version,
                    "date": release_date or now.date().isoformat(),
                    "downloadURL": current_download_url,
                    "size": metadata.size,
                }
                if metadata.minimum_os_version:
                    version["minOSVersion"] = metadata.minimum_os_version
                if row["version_description"]:
                    version["localizedDescription"] = row["version_description"]
                versions.append(version)

        row.update(
            {
                "version": metadata.version,
                "ipa_size": metadata.size,
                "minimum_os_version": metadata.minimum_os_version,
                "icon": (
                    base64.b64encode(metadata.icon).decode("ascii")
                    if metadata.icon
                    else row["icon"]
                ),
                "icon_content_type": metadata.icon_content_type or row["icon_content_type"],
                "name": (
                    row["name"]
                    if row["name"] != row["bundle_identifier"]
                    else metadata.name
                ),
                "developer_name": row["developer_name"] or metadata.developer_name,
                "last_checked": _iso(now),
                "next_check": _iso(now + timedelta(hours=row["poll_interval_hours"])),
                "last_error": "",
                "source_url": current_download_url,
                "release_tag": release_tag or row["release_tag"],
                "release_asset_name": release_asset_name or row["release_asset_name"],
                "versions": versions,
            }
        )
        updated = _app_to_dict(row)
    return _app_to_dict(updated)


def _check_app(app_id: str) -> dict[str, Any]:
    row = _get_app(app_id)
    try:
        if row["source_type"] == "github":
            assets = latest_ipa_assets(row["source_reference"])
            asset_index = row["asset_index"]
            if asset_index is None or asset_index >= len(assets):
                raise GitHubReleaseError(
                    "The latest GitHub release has fewer IPA assets than the release "
                    "this listing was created from."
                )
            asset = assets[asset_index]
            metadata = inspect_ipa(asset.download_url)
            return _apply_metadata(
                app_id,
                metadata,
                source_url=asset.download_url,
                release_tag=asset.release_tag,
                release_asset_name=asset.name,
                release_date=asset.release_date,
            )
        metadata = inspect_ipa(row["source_url"])
        return _apply_metadata(app_id, metadata)
    except Exception as exc:
        _save_error(app_id, exc)
        raise


async def _scheduler(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
            break
        except TimeoutError:
            pass
        now = _iso(_now())
        due_ids = [
            row["id"] for row in store.read()["apps"] if row["next_check"] <= now
        ]
        for app_id in due_ids:
            try:
                await run_in_threadpool(_check_app, app_id)
            except Exception:
                # The failure is persisted by _check_app and shown in the management UI.
                continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    stop_event = asyncio.Event()
    task = asyncio.create_task(_scheduler(stop_event))
    try:
        yield
    except asyncio.CancelledError:
        stop_event.set()
        current_task = asyncio.current_task()
        if current_task is not None:
            while current_task.cancelling():
                current_task.uncancel()
        await task
    finally:
        stop_event.set()
        if not task.done():
            await task


app = FastAPI(title="IPA Catalog", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


class CreateApp(BaseModel):
    ipa_url: str = Field(min_length=8, max_length=4096)
    developer_name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=10000)
    subtitle: str = Field(default="", max_length=200)
    version_description: str = Field(default="", max_length=10000)
    icon_url: str = Field(default="", max_length=4096)
    poll_interval_hours: int = Field(default=DEFAULT_INTERVAL_HOURS, ge=1, le=MAX_INTERVAL_HOURS)


class UpdateApp(BaseModel):
    source_url: str = Field(min_length=8, max_length=4096)
    name: str = Field(min_length=1, max_length=200)
    developer_name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=10000)
    subtitle: str = Field(default="", max_length=200)
    version_description: str = Field(default="", max_length=10000)
    icon_url: str = Field(default="", max_length=4096)
    poll_interval_hours: int = Field(ge=1, le=MAX_INTERVAL_HOURS)

    @field_validator("name")
    @classmethod
    def require_nonblank_labels(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This value cannot be blank.")
        return value


class StoreOption(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scheme: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z][a-zA-Z0-9+.-]*$")

    @field_validator("name", "scheme")
    @classmethod
    def require_nonblank_store_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This value cannot be blank.")
        return value


class UpdateSourceSettings(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    identifier: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10000)
    add_button_label: str = Field(min_length=1, max_length=60)
    store_options: list[StoreOption] = Field(max_length=10)

    @field_validator("name", "identifier", "add_button_label")
    @classmethod
    def require_nonblank_source_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This value cannot be blank.")
        return value


@app.get("/")
def home() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "admin.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/source.json")
def source(request: Request) -> JSONResponse:
    return JSONResponse(_public_source(request))


@app.get("/api/apps")
def public_apps(request: Request) -> list[dict[str, Any]]:
    source_data = _public_source(request)
    return [
        {
            "bundle_identifier": item["bundleIdentifier"],
            "developer_name": item["developerName"],
            "description": item["localizedDescription"],
            "icon_url": item["iconURL"],
            "name": item["name"],
            "subtitle": item.get("subtitle", ""),
            "version": item["versions"][0]["version"],
        }
        for item in source_data["apps"]
    ]


@app.get("/api/public-config")
def public_config() -> dict[str, Any]:
    return _settings()


async def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected_token = os.environ.get("ADMIN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A valid admin token is required.")
    if not hmac.compare_digest(authorization[7:], expected_token):
        raise HTTPException(status_code=401, detail="The admin token is invalid.")


def _placeholder_icon_png() -> bytes:
    background = (188, 52, 66, 255)
    foreground = (255, 255, 255, 255)
    rows = bytearray()
    for y in range(128):
        rows.append(0)
        for x in range(128):
            is_mark = (
                38 <= x <= 90 and 34 <= y <= 44
                or 58 <= x <= 70 and 44 < y < 83
                or 38 <= x <= 90 and 83 <= y <= 94
            )
            rows.extend(foreground if is_mark else background)

    def chunk(name: bytes, content: bytes) -> bytes:
        body = name + content
        return struct.pack(">I", len(content)) + body + struct.pack(
            ">I", binascii.crc32(body) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 128, 128, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


@app.get("/icons/{app_id}")
def icon(app_id: str) -> Response:
    row = _get_app(app_id)
    if row["icon"]:
        return Response(
            content=base64.b64decode(row["icon"]),
            media_type=row["icon_content_type"] or "application/octet-stream",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    return Response(
        content=_placeholder_icon_png(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/admin/apps", dependencies=[Depends(require_admin)])
def list_apps() -> list[dict[str, Any]]:
    rows = sorted(store.read()["apps"], key=lambda app: app["name"].casefold())
    return [_app_to_dict(row) for row in rows]


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
def update_source_settings(payload: UpdateSourceSettings) -> dict[str, Any]:
    settings = payload.model_dump()
    with store.edit() as state:
        state["source"] = settings
    return settings


def _insert_app(
    payload: CreateApp,
    metadata: IPAMetadata,
    *,
    source_type: str,
    source_reference: str,
    asset_index: int | None,
    source_url: str,
    release_tag: str = "",
    release_asset_name: str = "",
    release_date: str = "",
    release_asset_count: int = 1,
) -> dict[str, Any]:
    app_id = secrets.token_urlsafe(9)
    now = _now()
    version: dict[str, Any] = {
        "version": metadata.version,
        "date": release_date or now.date().isoformat(),
        "downloadURL": source_url,
        "size": metadata.size,
    }
    if metadata.minimum_os_version:
        version["minOSVersion"] = metadata.minimum_os_version
    if payload.version_description:
        version["localizedDescription"] = payload.version_description

    app = {
        "id": app_id,
        "source_url": source_url,
        "source_type": source_type,
        "source_reference": source_reference,
        "asset_index": asset_index,
        "release_tag": release_tag,
        "release_asset_name": release_asset_name,
        "bundle_identifier": metadata.bundle_identifier,
        "name": metadata.name,
        "developer_name": payload.developer_name.strip() or metadata.developer_name,
        "description": payload.description or metadata.description,
        "subtitle": payload.subtitle
        or (
            release_asset_name
            if source_type == "github" and release_asset_count > 1
            else ""
        ),
        "version_description": payload.version_description,
        "icon_url_override": payload.icon_url,
        "minimum_os_version": metadata.minimum_os_version,
        "version": metadata.version,
        "ipa_size": metadata.size,
        "icon": base64.b64encode(metadata.icon).decode("ascii") if metadata.icon else "",
        "icon_content_type": metadata.icon_content_type or "",
        "poll_interval_hours": payload.poll_interval_hours,
        "last_checked": _iso(now),
        "next_check": _iso(now + timedelta(hours=payload.poll_interval_hours)),
        "last_error": "",
        "versions": [version],
    }
    with store.edit() as state:
        state["apps"].append(app)
    return _app_to_dict(app)


@app.post("/api/admin/apps", status_code=201, dependencies=[Depends(require_admin)])
def create_app(payload: CreateApp) -> dict[str, Any]:
    try:
        hostname = urlsplit(payload.ipa_url).hostname
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="The IPA or GitHub URL is invalid.") from exc

    try:
        if hostname == "github.com":
            repository_url, _, _ = parse_repository_url(payload.ipa_url)
            assets = latest_ipa_assets(repository_url)
            inspected_assets = []
            for index, asset in enumerate(assets):
                metadata = inspect_ipa(asset.download_url)
                inspected_assets.append((index, asset, metadata))
            apps = []
            for index, asset, metadata in inspected_assets:
                apps.append(
                    _insert_app(
                        payload,
                        metadata,
                        source_type="github",
                        source_reference=repository_url,
                        asset_index=index,
                        source_url=asset.download_url,
                        release_tag=asset.release_tag,
                        release_asset_name=asset.name,
                        release_date=asset.release_date,
                        release_asset_count=len(assets),
                    )
                )
            return {"apps": apps, "count": len(apps)}

        metadata = inspect_ipa(payload.ipa_url)
    except (IPAError, GitHubReleaseError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    app = _insert_app(
        payload,
        metadata,
        source_type="ipa",
        source_reference=payload.ipa_url,
        asset_index=None,
        source_url=payload.ipa_url,
    )
    return {"apps": [app], "count": 1}


@app.put("/api/admin/apps/{app_id}", dependencies=[Depends(require_admin)])
def update_app(app_id: str, payload: UpdateApp) -> dict[str, Any]:
    row = _get_app(app_id)
    next_check = _iso(_now() + timedelta(hours=payload.poll_interval_hours))
    try:
        hostname = urlsplit(payload.source_url).hostname
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="The IPA or GitHub URL is invalid.") from exc

    source_type = "ipa"
    source_reference = payload.source_url
    asset_index = None
    release_tag = ""
    release_asset_name = ""
    source_url = payload.source_url
    if hostname == "github.com":
        try:
            source_reference, _, _ = parse_repository_url(payload.source_url)
            assets = latest_ipa_assets(source_reference)
            asset_index = (
                row["asset_index"]
                if row["source_type"] == "github"
                and row["source_reference"] == source_reference
                else 0
            )
            if asset_index is None or asset_index >= len(assets):
                raise GitHubReleaseError(
                    "The latest GitHub release does not have a matching IPA asset."
                )
            asset = assets[asset_index]
            source_type = "github"
            source_url = asset.download_url
            release_tag = asset.release_tag
            release_asset_name = asset.name
        except GitHubReleaseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    versions = row["versions"]
    if versions:
        versions[0]["downloadURL"] = source_url
        if payload.version_description:
            versions[0]["localizedDescription"] = payload.version_description
        else:
            versions[0].pop("localizedDescription", None)
    with store.edit() as state:
        target = next(app for app in state["apps"] if app["id"] == row["id"])
        target.update(
            {
                "source_url": source_url,
                "source_type": source_type,
                "source_reference": source_reference,
                "asset_index": asset_index,
                "release_tag": release_tag,
                "release_asset_name": release_asset_name,
                "name": payload.name.strip(),
                "developer_name": payload.developer_name.strip(),
                "description": payload.description,
                "subtitle": payload.subtitle,
                "version_description": payload.version_description,
                "icon_url_override": payload.icon_url,
                "poll_interval_hours": payload.poll_interval_hours,
                "next_check": next_check,
                "versions": versions,
            }
        )
    return _app_to_dict(_get_app(app_id))


@app.post("/api/admin/apps/{app_id}/check", dependencies=[Depends(require_admin)])
def check_app(app_id: str) -> dict[str, Any]:
    try:
        return _check_app(app_id)
    except (IPAError, GitHubReleaseError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/admin/apps/{app_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_app(app_id: str) -> Response:
    with store.edit() as state:
        remaining = [app for app in state["apps"] if app["id"] != app_id]
        if len(remaining) == len(state["apps"]):
            raise HTTPException(status_code=404, detail="App not found.")
        state["apps"] = remaining
    return Response(status_code=204)
