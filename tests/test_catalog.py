import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import main
from app.ipa import IPAMetadata
from app.github import ReleaseAsset
from app.state import CatalogStore


def metadata(version):
    return IPAMetadata(
        bundle_identifier="com.example.demo",
        version=version,
        name="Demo",
        developer_name="Demo Developer",
        minimum_os_version="15.0",
        size=1234,
        icon=b"image",
        icon_content_type="image/png",
        description="",
    )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_store = main.store
        main.store = CatalogStore(str(Path(self.temp_dir.name) / "catalog.yaml"))
        main.store.initialize()

    def tearDown(self):
        main.store = self.previous_store
        self.temp_dir.cleanup()

    def add_app(self):
        payload = main.CreateApp(
            ipa_url="https://downloads.example/demo.ipa",
            developer_name="Example",
            description="A demo app",
        )
        with patch("app.main.inspect_ipa", return_value=metadata("1.0")):
            return main.create_app(payload)["apps"][0]

    def test_source_has_compatible_fields_and_newest_version_first(self):
        app = self.add_app()
        with patch("app.main.inspect_ipa", return_value=metadata("2.0")):
            main._check_app(app["id"])

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("catalog.example", 443),
                "path": "/source.json",
                "headers": [],
                "query_string": b"",
                "root_path": "",
            }
        )
        source = main._public_source(request)

        self.assertEqual(source["identifier"], "com.example.ipacatalog")
        self.assertEqual(source["apps"][0]["bundleIdentifier"], "com.example.demo")
        self.assertEqual(source["apps"][0]["developerName"], "Example")
        self.assertEqual(source["apps"][0]["versions"][0]["version"], "2.0")
        self.assertEqual(source["apps"][0]["versions"][1]["version"], "1.0")
        self.assertEqual(source["apps"][0]["versions"][0]["downloadURL"], app["source_url"])

    def test_update_changes_download_url_and_current_version_notes(self):
        app = self.add_app()
        payload = main.UpdateApp(
            source_url="https://cdn.example/demo-v2.ipa",
            name="Renamed Demo",
            developer_name="Example",
            description="Updated description",
            version_description="Bug fixes",
            poll_interval_hours=12,
        )

        updated = main.update_app(app["id"], payload)
        source = main._public_source(
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "scheme": "https",
                    "server": ("catalog.example", 443),
                    "path": "/source.json",
                    "headers": [],
                    "query_string": b"",
                    "root_path": "",
                }
            )
        )

        self.assertEqual(updated["source_url"], payload.source_url)
        self.assertEqual(updated["poll_interval_hours"], 12)
        self.assertEqual(source["apps"][0]["name"], "Renamed Demo")
        self.assertEqual(
            source["apps"][0]["versions"][0]["downloadURL"], payload.source_url
        )
        self.assertEqual(
            source["apps"][0]["versions"][0]["localizedDescription"], "Bug fixes"
        )

    def test_source_settings_come_from_environment(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("catalog.example", 443),
                "path": "/source.json",
                "headers": [],
                "query_string": b"",
                "root_path": "",
            }
        )
        with patch.dict(
            os.environ,
            {
                "SOURCE_NAME": "Apps for Testing",
                "SOURCE_IDENTIFIER": "org.example.catalog",
            },
        ):
            source = main._public_source(request)
        self.assertEqual(source["name"], "Apps for Testing")
        self.assertEqual(source["identifier"], "org.example.catalog")

    def test_developer_name_can_be_omitted_and_is_read_from_ipa(self):
        payload = main.CreateApp(ipa_url="https://downloads.example/demo.ipa")
        with patch("app.main.inspect_ipa", return_value=metadata("1.0")):
            app = main.create_app(payload)["apps"][0]

        self.assertEqual(app["developer_name"], "Demo Developer")

    def test_github_repository_adds_every_ipa_asset(self):
        payload = main.CreateApp(ipa_url="https://github.com/example/demo")
        assets = [
            ReleaseAsset(
                name="Demo.ipa",
                download_url="https://github.com/example/demo/releases/download/v1/Demo.ipa",
                release_tag="v1",
                release_date="2026-10-01",
            ),
            ReleaseAsset(
                name="Demo-iOS16.ipa",
                download_url="https://github.com/example/demo/releases/download/v1/Demo-iOS16.ipa",
                release_tag="v1",
                release_date="2026-10-01",
            ),
        ]
        with (
            patch("app.main.latest_ipa_assets", return_value=assets),
            patch("app.main.inspect_ipa", side_effect=[metadata("1.0"), metadata("1.0")]),
        ):
            result = main.create_app(payload)

        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["apps"]), 2)
        self.assertEqual([app["asset_index"] for app in result["apps"]], [0, 1])
        self.assertEqual(
            result["apps"][1]["source_url"], assets[1].download_url
        )
        self.assertEqual(
            result["apps"][1]["source_reference"], "https://github.com/example/demo"
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("catalog.example", 443),
                "path": "/source.json",
                "headers": [],
                "query_string": b"",
                "root_path": "",
            }
        )
        source_apps = main._public_source(request)["apps"]
        self.assertEqual(len(source_apps), 1)
        self.assertEqual(
            {
                version["downloadURL"]
                for version in source_apps[0]["versions"]
            },
            {asset.download_url for asset in assets},
        )
        self.assertIn("Variants:", source_apps[0]["subtitle"])

    def test_github_check_refreshes_latest_release_download_url(self):
        payload = main.CreateApp(ipa_url="https://github.com/example/demo")
        old_asset = ReleaseAsset(
            name="Demo.ipa",
            download_url="https://github.com/example/demo/releases/download/v1/Demo.ipa",
            release_tag="v1",
            release_date="2026-10-01",
        )
        new_asset = ReleaseAsset(
            name="Demo.ipa",
            download_url="https://github.com/example/demo/releases/download/v2/Demo.ipa",
            release_tag="v2",
            release_date="2026-10-10",
        )
        with (
            patch("app.main.latest_ipa_assets", return_value=[old_asset]),
            patch("app.main.inspect_ipa", return_value=metadata("1.0")),
        ):
            app = main.create_app(payload)["apps"][0]
        with (
            patch("app.main.latest_ipa_assets", return_value=[new_asset]),
            patch("app.main.inspect_ipa", return_value=metadata("2.0")),
        ):
            updated = main._check_app(app["id"])

        self.assertEqual(updated["release_tag"], "v2")
        self.assertEqual(updated["source_url"], new_asset.download_url)
        versions = main._get_app(app["id"])["versions"]
        self.assertEqual(versions[0]["version"], "2.0")
        self.assertEqual(versions[0]["downloadURL"], new_asset.download_url)

    def test_admin_api_requires_token_and_public_feed_remains_open(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "test-token"}):
            with TestClient(main.app) as client:
                self.assertEqual(client.get("/admin").status_code, 200)
                self.assertEqual(client.get("/api/apps").status_code, 200)
                self.assertEqual(client.get("/api/admin/apps").status_code, 401)
                self.assertEqual(
                    client.put("/api/admin/settings", json={"name": "no auth"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.get(
                        "/api/admin/apps",
                        headers={"Authorization": "Bearer wrong-token"},
                    ).status_code,
                    401,
                )
                response = client.get(
                    "/api/admin/apps",
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])
                with patch.dict(os.environ, {"ADMIN_TOKEN": ""}):
                    self.assertEqual(
                        client.get(
                            "/api/admin/apps",
                            headers={"Authorization": "Bearer test-token"},
                        ).status_code,
                        503,
                    )

    def test_catalog_state_persists_as_yaml(self):
        app = self.add_app()
        reloaded = CatalogStore(str(main.store.path))
        reloaded.initialize()
        self.assertEqual(reloaded.read()["apps"][0]["id"], app["id"])
        self.assertIn("apps:", main.store.path.read_text(encoding="utf-8"))

    def test_admin_can_update_and_persist_source_details(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "test-token"}):
            with TestClient(main.app) as client:
                response = client.put(
                    "/api/admin/settings",
                    headers={"Authorization": "Bearer test-token"},
                    json={
                        "name": "Community Apps",
                        "identifier": "org.example.community",
                        "description": "A curated collection of apps.",
                        "add_button_label": "Install with",
                        "store_options": [
                            {"name": "MyStore", "scheme": "mystore"},
                            {"name": "Alternate", "scheme": "alternate+store"},
                        ],
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["name"], "Community Apps")
                self.assertEqual(response.json()["add_button_label"], "Install with")
                self.assertEqual(
                    client.get("/api/public-config").json(),
                    {
                        "name": "Community Apps",
                        "identifier": "org.example.community",
                        "description": "A curated collection of apps.",
                        "add_button_label": "Install with",
                        "store_options": [
                            {"name": "MyStore", "scheme": "mystore"},
                            {"name": "Alternate", "scheme": "alternate+store"},
                        ],
                    },
                )
                self.assertEqual(
                    client.get("/source.json").json()["identifier"],
                    "org.example.community",
                )

        persisted = CatalogStore(str(main.store.path)).read()["source"]
        self.assertEqual(persisted["description"], "A curated collection of apps.")
        self.assertEqual(persisted["store_options"][0]["scheme"], "mystore")

    def test_admin_rejects_invalid_store_url_scheme(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": "test-token"}):
            with TestClient(main.app) as client:
                response = client.put(
                    "/api/admin/settings",
                    headers={"Authorization": "Bearer test-token"},
                    json={
                        "name": "Catalog",
                        "identifier": "org.example.catalog",
                        "add_button_label": "Add with",
                        "store_options": [{"name": "Unsafe", "scheme": "https://evil.test"}],
                    },
                )
        self.assertEqual(response.status_code, 422)


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_stops_scheduler_without_cancelling_it(self):
        with patch.object(main.store, "initialize"):
            async with main.lifespan(main.app):
                pass

        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        self.assertEqual(pending, [])

    async def test_lifespan_cancellation_stops_scheduler_without_error(self):
        async def run_lifespan():
            async with main.lifespan(main.app):
                await asyncio.Event().wait()

        with patch.object(main.store, "initialize"):
            task = asyncio.create_task(run_lifespan())
            await asyncio.sleep(0)
            task.cancel()
            task.cancel()
            await task

        pending = [
            pending_task
            for pending_task in asyncio.all_tasks()
            if pending_task is not asyncio.current_task() and not pending_task.done()
        ]
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
