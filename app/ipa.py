"""Download and inspect IPA archives."""

from __future__ import annotations

import http.client
import ipaddress
import os
import plistlib
import socket
import ssl
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

MAX_IPA_SIZE = 512 * 1024 * 1024
MAX_PLIST_SIZE = 16 * 1024 * 1024
MAX_ICON_SIZE = 10 * 1024 * 1024


class IPAError(ValueError):
    """Raised when an IPA cannot be downloaded or inspected."""


@dataclass(frozen=True)
class IPAMetadata:
    bundle_identifier: str
    version: str
    name: str
    developer_name: str
    minimum_os_version: str | None
    size: int
    icon: bytes | None
    icon_content_type: str | None
    description: str


def _resolve_public_url(
    url: str,
) -> tuple[urllib.parse.SplitResult, tuple[int, int, int, tuple]]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise IPAError("The IPA URL is invalid.") from exc

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise IPAError("The IPA URL must use HTTP or HTTPS.")
    if parsed.username or parsed.password:
        raise IPAError("Credentials in IPA URLs are not supported.")
    if port is not None and not 1 <= port <= 65535:
        raise IPAError("The IPA URL has an invalid port.")

    try:
        resolved = socket.getaddrinfo(
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, ValueError) as exc:
        raise IPAError("The IPA host could not be resolved.") from exc
    if not resolved or any(
        not ipaddress.ip_address(result[4][0]).is_global for result in resolved
    ):
        raise IPAError("The IPA URL must resolve only to public IP addresses.")
    family, socktype, proto, _, sockaddr = resolved[0]
    return parsed, (family, socktype, proto, sockaddr)


def _connect_to_address(
    address_info: tuple[int, int, int, tuple], timeout: float | None
) -> socket.socket:
    family, socktype, proto, sockaddr = address_info
    connection = socket.socket(family, socktype, proto)
    try:
        connection.settimeout(timeout)
        connection.connect(sockaddr)
    except BaseException:
        connection.close()
        raise
    return connection


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address_info, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address_info = address_info

    def connect(self):
        self.sock = _connect_to_address(self.address_info, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address_info, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address_info = address_info

    def connect(self):
        sock = _connect_to_address(self.address_info, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _open_response(url: str):
    parsed, address_info = _resolve_public_url(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = (
        _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    )
    connection = connection_type(parsed.hostname, port, address_info, timeout=30)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Host": parsed.netloc,
                "User-Agent": "IPA-Catalog/1.0",
                "Accept": "application/octet-stream",
                "Connection": "close",
            },
        )
        return connection, connection.getresponse()
    except BaseException:
        connection.close()
        raise


def _download(url: str, target: BinaryIO) -> int:
    connection = None
    try:
        for redirect_count in range(11):
            connection, response = _open_response(url)
            if response.status not in (301, 302, 303, 307, 308):
                break
            location = response.getheader("Location")
            response.close()
            connection.close()
            connection = None
            if not location:
                raise IPAError("The IPA URL redirected without a destination.")
            if redirect_count == 10:
                raise IPAError("The IPA URL redirected too many times.")
            url = urllib.parse.urljoin(url, location)

        with response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IPA_SIZE:
                raise IPAError("The IPA exceeds the 512 MiB size limit.")
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_IPA_SIZE:
                    raise IPAError("The IPA exceeds the 512 MiB size limit.")
                target.write(chunk)
            if size == 0:
                raise IPAError("The IPA URL returned an empty file.")
            return size
    except IPAError:
        raise
    except (http.client.HTTPException, ssl.SSLError, TimeoutError, OSError, ValueError) as exc:
        raise IPAError(f"Could not download the IPA: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _app_bundle_path(archive: zipfile.ZipFile) -> str:
    candidates = [
        item.filename
        for item in archive.infolist()
        if item.filename.startswith("Payload/")
        and item.filename.count("/") == 2
        and item.filename.endswith(".app/Info.plist")
        and not item.filename.startswith("Payload/Watch/")
    ]
    if not candidates:
        raise IPAError("The file is not a valid IPA: Payload/*.app/Info.plist was not found.")
    return sorted(candidates)[0]


def _icon_path(archive: zipfile.ZipFile, app_dir: str, plist: dict) -> str | None:
    icon_names: list[str] = []
    files = plist.get("CFBundleIconFiles", [])
    if isinstance(files, list):
        icon_names.extend(item for item in files if isinstance(item, str))
    icon_sets = plist.get("CFBundleIcons", {})
    if isinstance(icon_sets, dict):
        primary = icon_sets.get("CFBundlePrimaryIcon", {})
        if isinstance(primary, dict) and isinstance(primary.get("CFBundleIconFiles"), list):
            icon_names.extend(
                item for item in primary["CFBundleIconFiles"] if isinstance(item, str)
            )

    zip_names = set(archive.namelist())
    for name in reversed(icon_names):
        for suffix in ("", ".png", "@2x.png", "@3x.png", ".jpg", ".jpeg"):
            candidate = f"{app_dir}{name}{suffix}"
            if candidate in zip_names and candidate.lower().endswith((".png", ".jpg", ".jpeg")):
                return candidate

    candidates = sorted(
        path
        for path in zip_names
        if path.startswith(app_dir)
        and path.lower().endswith((".png", ".jpg", ".jpeg"))
        and "icon" in os.path.basename(path).lower()
    )
    return candidates[-1] if candidates else None


def _parse_archive(file: BinaryIO, size: int) -> IPAMetadata:
    try:
        with zipfile.ZipFile(file) as archive:
            plist_path = _app_bundle_path(archive)
            info = archive.getinfo(plist_path)
            if info.file_size > MAX_PLIST_SIZE:
                raise IPAError("The IPA contains an oversized Info.plist.")
            plist = plistlib.loads(archive.read(info))
            if not isinstance(plist, dict):
                raise IPAError("The IPA contains an invalid Info.plist.")
            bundle_identifier = plist.get("CFBundleIdentifier")
            version = plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")
            if not isinstance(bundle_identifier, str) or not bundle_identifier.strip():
                raise IPAError("CFBundleIdentifier is missing from Info.plist.")
            if not isinstance(version, str) or not version.strip():
                raise IPAError("CFBundleShortVersionString is missing from Info.plist.")

            developer_name = (
                plist.get("DeveloperName")
                or plist.get("CFBundleDeveloperName")
                or ""
            )
            try:
                store_metadata_info = archive.getinfo("iTunesMetadata.plist")
            except KeyError:
                store_metadata_info = None
            if store_metadata_info and store_metadata_info.file_size <= MAX_PLIST_SIZE:
                try:
                    store_metadata = plistlib.loads(archive.read(store_metadata_info))
                except plistlib.InvalidFileException:
                    store_metadata = {}
                if isinstance(store_metadata, dict):
                    developer_name = (
                        developer_name
                        or store_metadata.get("artistName")
                        or store_metadata.get("sellerName")
                        or ""
                    )

            name = (
                plist.get("CFBundleDisplayName")
                or plist.get("CFBundleName")
                or bundle_identifier
            )
            description = plist.get("CFBundleGetInfoString", "")
            icon = None
            icon_content_type = None
            icon_path = _icon_path(archive, plist_path.rsplit("/", 1)[0] + "/", plist)
            if icon_path:
                icon_info = archive.getinfo(icon_path)
                if icon_info.file_size <= MAX_ICON_SIZE:
                    icon = archive.read(icon_info)
                    icon_content_type = (
                        "image/png"
                        if icon_path.lower().endswith(".png")
                        else "image/jpeg"
                    )

            minimum_os = plist.get("MinimumOSVersion")
            return IPAMetadata(
                bundle_identifier=bundle_identifier.strip(),
                version=version.strip(),
                name=str(name).strip(),
                developer_name=str(developer_name).strip(),
                minimum_os_version=str(minimum_os) if minimum_os else None,
                size=size,
                icon=icon,
                icon_content_type=icon_content_type,
                description=str(description),
            )
    except IPAError:
        raise
    except (zipfile.BadZipFile, KeyError, plistlib.InvalidFileException, OSError) as exc:
        raise IPAError("The download is not a readable IPA archive.") from exc


def inspect_ipa(url: str) -> IPAMetadata:
    """Fetch an IPA URL and return the app metadata embedded in the archive."""
    with tempfile.TemporaryFile() as file:
        size = _download(url, file)
        file.seek(0)
        return _parse_archive(file, size)
