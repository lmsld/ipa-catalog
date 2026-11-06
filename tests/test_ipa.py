import io
import socket
import plistlib
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from app.ipa import (
    IPAError,
    _connect_to_address,
    _download,
    _parse_archive,
    _resolve_public_url,
)
from app.main import _placeholder_icon_png


class ParseIPATests(unittest.TestCase):
    def archive(self, plist, store_metadata=None):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr(
                "Payload/Demo.app/Info.plist",
                plistlib.dumps(plist),
            )
            archive.writestr("Payload/Demo.app/Icon.png", b"png-data")
            if store_metadata is not None:
                archive.writestr(
                    "iTunesMetadata.plist",
                    plistlib.dumps(store_metadata),
                )
        contents.seek(0)
        return contents

    def test_extracts_bundle_version_name_and_icon(self):
        ipa = self.archive(
            {
                "CFBundleIdentifier": "com.example.demo",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleDisplayName": "Demo App",
                "DeveloperName": "Demo Developer",
                "CFBundleIconFiles": ["Icon"],
                "MinimumOSVersion": "15.0",
            }
        )

        metadata = _parse_archive(ipa, 1234)

        self.assertEqual(metadata.bundle_identifier, "com.example.demo")
        self.assertEqual(metadata.version, "1.2.3")
        self.assertEqual(metadata.name, "Demo App")
        self.assertEqual(metadata.developer_name, "Demo Developer")
        self.assertEqual(metadata.minimum_os_version, "15.0")
        self.assertEqual(metadata.size, 1234)
        self.assertEqual(metadata.icon, b"png-data")
        self.assertEqual(metadata.icon_content_type, "image/png")

    def test_extracts_developer_from_itunes_metadata(self):
        contents = self.archive(
            {
                "CFBundleIdentifier": "com.example.demo",
                "CFBundleShortVersionString": "1.0",
            },
            {"artistName": "Store Developer"},
        )

        self.assertEqual(_parse_archive(contents, 100).developer_name, "Store Developer")

    def test_rejects_archive_without_app_info_plist(self):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("Payload/readme.txt", "not an app")
        contents.seek(0)

        with self.assertRaisesRegex(IPAError, "Info.plist was not found"):
            _parse_archive(contents, 10)

    def test_rejects_private_download_hosts(self):
        with self.assertRaisesRegex(IPAError, "public IP addresses"):
            _resolve_public_url("http://127.0.0.1/app.ipa")

    def test_connects_to_the_address_that_was_validated(self):
        validated_address = ("1.1.1.1", 80)
        address_info = (socket.AF_INET, socket.SOCK_STREAM, 6, "", validated_address)
        with (
            patch("app.ipa.socket.getaddrinfo", return_value=[address_info]) as resolve,
            patch("app.ipa.socket.socket") as create_socket,
        ):
            _, pinned_address = _resolve_public_url("http://example.test/app.ipa")
            connection = create_socket.return_value
            _connect_to_address(pinned_address, 30)

        resolve.assert_called_once()
        connection.connect.assert_called_once_with(validated_address)

    def test_redirect_to_private_address_is_rejected(self):
        initial_url = "https://example.test/app.ipa"
        private_url = "http://127.0.0.1/private.ipa"
        connection = MagicMock()
        response = MagicMock()
        response.status = 302
        response.getheader.return_value = private_url
        response.close.return_value = None

        from app.ipa import _open_response

        def open_response(url):
            if url == initial_url:
                return connection, response
            return _open_response(url)

        with patch("app.ipa._open_response", side_effect=open_response):
            with self.assertRaisesRegex(IPAError, "public IP addresses"):
                _download(initial_url, io.BytesIO())
        connection.close.assert_called_once()

    def test_fallback_icon_is_png(self):
        self.assertTrue(_placeholder_icon_png().startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
