"""Browser regression test for upgrading representative FTMS Bike databases.

Run with an existing Python Playwright install and Chromium browser:
    py -3.11 tests/test_indexeddb_migration.py

The test uses an isolated browser context and an in-process HTTP server. It does
not access Supabase or require network access to the Supabase JS CDN.
"""

from __future__ import annotations

import functools
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "ftms-bike-db-v2"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


class IndexedDBMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        handler = functools.partial(QuietHandler, directory=str(REPO_ROOT))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def seed_database(self, page, version: int, sessions: list[dict], active: list[dict]) -> None:
        page.evaluate(
            """async ({name, version, sessions, active}) => new Promise((resolve, reject) => {
              const request = indexedDB.open(name, version);
              request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains('sessions')) {
                  const store = db.createObjectStore('sessions', {keyPath: 'id'});
                  store.createIndex('start', 'start');
                }
                if (!db.objectStoreNames.contains('active')) {
                  db.createObjectStore('active', {keyPath: 'id'});
                }
              };
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const db = request.result;
                const tx = db.transaction(['sessions', 'active'], 'readwrite');
                for (const value of sessions) tx.objectStore('sessions').put(value);
                for (const value of active) tx.objectStore('active').put(value);
                tx.oncomplete = () => { db.close(); resolve(true); };
                tx.onerror = () => reject(tx.error);
              };
            })""",
            {"name": DB_NAME, "version": version, "sessions": sessions, "active": active},
        )

    def read_stores(self, page) -> dict:
        return page.evaluate(
            """name => new Promise((resolve, reject) => {
              const request = indexedDB.open(name);
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const db = request.result;
                const tx = db.transaction(['sessions', 'active'], 'readonly');
                const sessions = tx.objectStore('sessions').getAll();
                const active = tx.objectStore('active').getAll();
                tx.oncomplete = () => {
                  const result = {version: db.version, sessions: sessions.result, active: active.result};
                  db.close();
                  resolve(result);
                };
                tx.onerror = () => reject(tx.error);
              };
            })""",
            DB_NAME,
        )

    @staticmethod
    def typed_id(row: dict) -> tuple[str, str]:
        return (type(row["id"]).__name__, str(row["id"]))

    def run_upgrade_case(self, version: int, sessions: list[dict], active: list[dict]) -> None:
        context = self.browser.new_context(service_workers="block")
        context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
        page = context.new_page()
        try:
            # Load a same-origin static resource without executing the app first.
            page.goto(f"{self.base_url}/sw.js", wait_until="domcontentloaded")
            self.seed_database(page, version, sessions, active)
            page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
            page.wait_for_function(
                "async name => (await indexedDB.databases()).some(db => db.name === name && db.version === 3)",
                arg=DB_NAME,
            )
            # Wait until checkRecovery and local-only auth initialization finish.
            page.wait_for_function("document.querySelector('#authMessage')?.textContent.startsWith('Configura')")

            upgraded = self.read_stores(page)
            self.assertEqual(upgraded["version"], 3)
            self.assertEqual(len(upgraded["sessions"]), len(sessions))
            self.assertCountEqual(
                [self.typed_id(row) for row in upgraded["sessions"]],
                [self.typed_id(row) for row in sessions],
            )
            for row in upgraded["sessions"]:
                original = next(item for item in sessions if self.typed_id(item) == self.typed_id(row))
                self.assertEqual(row["id"], original["id"])
                if "ownerId" not in original:
                    self.assertIsNone(row["ownerId"])
                    self.assertEqual(row["syncState"], "local")
                else:
                    self.assertEqual(row.get("ownerId"), original.get("ownerId"))
                    self.assertEqual(row.get("syncState"), original.get("syncState"))

            stored_keys = {self.typed_id(row) for row in sessions}
            legacy_recoverable = [
                row for row in active
                if row["id"] != "active" and self.typed_id(row) not in stored_keys
            ]
            if legacy_recoverable:
                source = max(legacy_recoverable, key=lambda row: row.get("lastUpdated", row.get("start", 0)))
                recovered = next((row for row in upgraded["active"] if row["id"] == "active"), None)
                self.assertIsNotNone(recovered)
                self.assertFalse(any(row["id"] == source["id"] for row in upgraded["active"]))
                self.assertEqual(recovered["samples"], source["samples"])
                self.assertIsInstance(recovered["sessionId"], str)
            if any(row["id"] == "active" for row in upgraded["active"]):
                page.wait_for_function("!document.querySelector('#resumeBox').classList.contains('hidden')")
                self.assertTrue(page.locator("#resumeBox").is_visible())
            for completed_copy in active:
                if self.typed_id(completed_copy) in stored_keys:
                    retained = next(
                        row for row in upgraded["active"]
                        if self.typed_id(row) == self.typed_id(completed_copy)
                    )
                    self.assertEqual(retained["samples"], completed_copy["samples"])

            visible_count = sum(row.get("ownerId") is None for row in sessions)
            page.locator("#openHistory").click()
            page.locator(".session-row").first.wait_for()
            self.assertEqual(page.locator(".session-row").count(), visible_count)
            for index in range(visible_count):
                page.locator(".session-row").nth(index).click()
                page.wait_for_function("!document.querySelector('#detailScreen').classList.contains('hidden')")
                self.assertTrue(page.locator("#detailScreen").is_visible())
                page.locator("#backFromDetail").click()
                page.locator(".session-row").nth(index).wait_for()

            # Reopening v3 must not duplicate or rewrite any local session key.
            page.reload(wait_until="domcontentloaded")
            second_read = self.read_stores(page)
            self.assertEqual(len(second_read["sessions"]), len(sessions))
            self.assertCountEqual(
                [self.typed_id(row) for row in second_read["sessions"]],
                [self.typed_id(row) for row in sessions],
            )
            numeric_match = next(
                (row for row in sessions if isinstance(row["id"], int)
                 and any(isinstance(other["id"], str) and other["id"] == str(row["id"])
                         for other in sessions)),
                None,
            )
            if numeric_match:
                page.locator("#openHistory").click()
                string_match = next(
                    row for row in sessions
                    if isinstance(row["id"], str) and row["id"] == str(numeric_match["id"])
                )
                visible_ids = [row for row in sessions if row.get("ownerId") is None]
                target_index = next(
                    index for index, row in enumerate(visible_ids)
                    if self.typed_id(row) == self.typed_id(string_match)
                )
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator(".session-row").nth(target_index).locator(".deleteOne").click()
                page.wait_for_function(
                    "count => document.querySelectorAll('.session-row').length === count",
                    arg=visible_count - 1,
                )
                after_delete = self.read_stores(page)
                remaining_ids = [self.typed_id(row) for row in after_delete["sessions"]]
                self.assertNotIn(self.typed_id(string_match), remaining_ids)
                self.assertIn(self.typed_id(numeric_match), remaining_ids)
        finally:
            context.close()

    def test_upgrade_from_version_1_keeps_numeric_keys_and_history_queryable(self) -> None:
        sessions = [
            {"id": 1710000000001, "start": 1710000000001, "duration": 60, "samples": []},
            {"id": 1710000000002, "start": 1710000000002, "duration": 90, "samples": []},
        ]
        active = [
            {"id": 1710000000001, "start": 1710000000001, "lastUpdated": 1710000002000,
             "elapsedSec": 60, "distanceKm": 0, "samples": [{"t": 60, "power": 90}]},
            {"id": 1710000000999, "start": 1710000000999, "lastUpdated": 1710000001000,
             "elapsedSec": 20, "distanceKm": 0, "samples": [{"t": 20, "power": 110}]},
        ]
        self.run_upgrade_case(1, sessions, active)

    def test_fresh_empty_database_opens_with_both_stores(self) -> None:
        context = self.browser.new_context(service_workers="block")
        context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
        page = context.new_page()
        try:
            page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
            page.wait_for_function(
                "async name => (await indexedDB.databases()).some(db => db.name === name && db.version === 3)",
                arg=DB_NAME,
            )
            result = self.read_stores(page)
            self.assertEqual(result["version"], 3)
            self.assertEqual(result["sessions"], [])
            self.assertEqual(result["active"], [])
        finally:
            context.close()

    def test_service_worker_caches_network_response_before_offline_use(self) -> None:
        context = self.browser.new_context(service_workers="allow")
        context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
        context.route(
            "**/offline-probe.txt",
            lambda route: route.fulfill(status=200, body="cached-response"),
        )
        page = context.new_page()
        try:
            page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
            page.wait_for_function("!!navigator.serviceWorker?.controller")
            self.assertEqual(
                page.evaluate("fetch('/offline-probe.txt').then(response => response.text())"),
                "cached-response",
            )
            context.unroute("**/offline-probe.txt")
            context.set_offline(True)
            self.assertEqual(
                page.evaluate("fetch('/offline-probe.txt').then(response => response.text())"),
                "cached-response",
            )
            page.reload(wait_until="domcontentloaded")
            self.assertTrue(page.locator("#authEmail").count())
        finally:
            context.close()

    def test_upgrade_from_version_2_keeps_legacy_and_existing_sync_rows(self) -> None:
        sessions = [
            {"id": 1710000000101, "start": 1710000000101, "duration": 60, "samples": []},
            {"id": "1710000000101", "start": 1710000000102,
             "ownerId": None, "syncState": "local", "duration": 65, "samples": []},
            {"id": "00000000-0000-4000-8000-000000000001", "start": 1710000000102,
             "ownerId": None, "syncState": "local", "duration": 70, "samples": []},
            {"id": "00000000-0000-4000-8000-000000000002", "start": 1710000000103,
             "ownerId": "test-user", "syncState": "pending", "duration": 80, "samples": []},
        ]
        active = [{"id": "active", "sessionId": "00000000-0000-4000-8000-000000000003",
                   "start": 1710000000999, "lastUpdated": 1710000001000,
                   "elapsedSec": 20, "distanceKm": 0, "samples": []}]
        self.run_upgrade_case(2, sessions, active)

    def test_finalized_session_removes_only_its_obsolete_active_record(self) -> None:
        session_id = "00000000-0000-4000-8000-000000000099"
        finished = {"id": session_id, "start": 1710000000000, "duration": 60,
                    "samples": [{"t": 60, "power": 125}], "ownerId": None, "syncState": "local"}
        stale_active = {"id": "active", "sessionId": session_id, "start": finished["start"],
                        "lastUpdated": 1710000000060, "elapsedSec": 60,
                        "distanceKm": 0, "samples": finished["samples"]}
        for version in (2, 3):
            context = self.browser.new_context(service_workers="block")
            context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
            page = context.new_page()
            try:
                page.goto(f"{self.base_url}/sw.js", wait_until="domcontentloaded")
                self.seed_database(page, version, [finished], [stale_active])
                page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
                page.wait_for_function("document.querySelector('#authMessage')?.textContent.startsWith('Configura')")
                page.wait_for_function("document.querySelector('#openHistory') !== null")
                self.assertFalse(page.locator("#resumeBox").is_visible())
                result = self.read_stores(page)
                self.assertEqual(len(result["sessions"]), 1)
                self.assertEqual(result["sessions"][0]["id"], session_id)
                self.assertEqual(result["sessions"][0]["samples"], finished["samples"])
                self.assertFalse(any(row["id"] == "active" for row in result["active"]))
            finally:
                context.close()

    def test_incomplete_active_without_session_id_is_kept_and_recoverable(self) -> None:
        active = {"id": "active", "start": 1710000000000, "lastUpdated": 1710000000020,
                  "elapsedSec": 20, "distanceKm": 0, "samples": [{"t": 20, "power": 100}]}
        context = self.browser.new_context(service_workers="block")
        context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
        page = context.new_page()
        try:
            page.goto(f"{self.base_url}/sw.js", wait_until="domcontentloaded")
            self.seed_database(page, 3, [], [active])
            page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#authMessage')?.textContent.startsWith('Configura')")
            self.assertTrue(page.locator("#resumeBox").is_visible())
            result = self.read_stores(page)
            self.assertEqual(result["sessions"], [])
            self.assertEqual(len(result["active"]), 1)
            self.assertEqual(result["active"][0]["samples"], active["samples"])
        finally:
            context.close()

    def test_supabase_global_does_not_conflict_and_signin_handler_runs(self) -> None:
        supabase_sdk = """
          var supabase = {createClient: () => ({auth: {
            getSession: async () => ({data: {session: null}}),
            onAuthStateChange: () => ({data: {subscription: {unsubscribe() {}}}}),
            signInWithOtp: async ({email}) => {window.__sentEmail = email; return {error: null};},
            signOut: async () => ({error: null})
          }})};
        """
        context = self.browser.new_context(service_workers="block")
        context.route(
            "https://cdn.jsdelivr.net/**",
            lambda route: route.fulfill(
                status=200, content_type="application/javascript", body=supabase_sdk
            ),
        )
        page = context.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('#authMessage')?.textContent.startsWith('Inicia sesión')"
            )
            self.assertFalse(page.locator("#signIn").is_disabled())
            page.locator("#authEmail").fill("rider@example.invalid")
            page.locator("#signIn").click()
            page.wait_for_function("window.__sentEmail === 'rider@example.invalid'")
            self.assertIn("Revisa tu correo", page.locator("#authMessage").inner_text())
            self.assertEqual(page_errors, [])
        finally:
            context.close()

    def test_authenticated_user_sees_local_history_without_uploading_it(self) -> None:
        fake_client = """
          window.__syncCalls = [];
          window.__remoteRows = [];
          window.supabase = { createClient: () => ({
            auth: {
              getSession: async () => ({data: {session: {user: {id: 'test-user', email: 'test@example.invalid'}}}}),
              onAuthStateChange: () => ({data: {subscription: {unsubscribe() {}}}}),
              signOut: async () => ({error: null}),
              signInWithOtp: async () => ({error: null})
            },
            from: () => ({
              upsert: async row => { window.__syncCalls.push({kind: 'upsert', id: row.id, user_id: row.user_id}); window.__remoteRows = window.__remoteRows.filter(existing => existing.id !== row.id).concat(row); return {error: null}; },
              delete: () => ({eq: async (column, value) => { window.__syncCalls.push({kind: 'delete', column, value}); return {error: null}; }}),
              select: () => { const query = {order() { return query; }, range: async () => ({data: window.__remoteRows, error: null})}; return query; }
            })
          })};
        """
        sessions = [
            {"id": 1710000000201, "start": 1710000000201, "duration": 60,
             "samples": [], "avgPower": 90},
            {"id": "00000000-0000-4000-8000-000000000201", "start": 1710000000202,
             "ownerId": None, "syncState": "local", "duration": 70, "samples": [], "avgPower": 95},
            {"id": "00000000-0000-4000-8000-000000000202", "start": 1710000000203,
             "ownerId": "test-user", "syncState": "pending", "duration": 80, "samples": [], "avgPower": 105},
        ]
        context = self.browser.new_context(service_workers="block")
        context.route(
            "https://cdn.jsdelivr.net/**",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=fake_client),
        )
        page = context.new_page()
        try:
            page.goto(f"{self.base_url}/sw.js", wait_until="domcontentloaded")
            self.seed_database(page, 2, sessions, [])
            page.goto(f"{self.base_url}/index.html", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#syncStatus')?.textContent === 'Sincronizado'")
            self.assertIn("test@example.invalid", page.locator("#authMessage").inner_text())

            calls = page.evaluate("window.__syncCalls")
            upserts = [call for call in calls if call["kind"] == "upsert"]
            self.assertEqual(len(upserts), 1)
            self.assertEqual(upserts[0]["id"], "00000000-0000-4000-8000-000000000202")
            self.assertEqual(upserts[0]["user_id"], "test-user")

            result = self.read_stores(page)
            legacy = next(row for row in result["sessions"] if isinstance(row["id"], int))
            self.assertIsNone(legacy["ownerId"])
            self.assertEqual(legacy["syncState"], "local")

            page.locator("#openHistory").click()
            page.evaluate("renderHistory()")
            diagnostics = page.evaluate("""async () => {
              const rows = await dbAll();
              return {authId: authUser?.id, owners: rows.map(row => row.ownerId),
                      visible: visibleSessions(rows).length,
                      rendered: document.querySelectorAll('.session-row').length};
            }""")
            self.assertEqual(page.locator(".session-row").count(), 3, str(diagnostics))
            page.locator('.session-row[data-id="number:1710000000201"]').click()
            page.wait_for_function("!document.querySelector('#detailScreen').classList.contains('hidden')")
            self.assertTrue(page.locator("#detailScreen").is_visible())
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
