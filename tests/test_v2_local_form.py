"""Local HTTP fixture tests use an isolated DEMO database and loopback only."""
from contextlib import closing, redirect_stderr
from copy import deepcopy
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from jobhunt.models import digest
from jobhunt.v2.models import Job, SourcePolicy


APP_ID = "APP-" + "a" * 32


class LocalFormTests(unittest.TestCase):
    def setUp(self):
        from jobhunt.v2.local_form import create_server
        from jobhunt.v2.application_store import ApplicationStore
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.server = create_server(self.home, 0)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.db_path = self.home / "data" / "v2" / "demo" / "business.sqlite3"
        job = Job(source="demo", external_id="p3", company="DEMO company",
                  title="DEMO Python", jd="</script><img src=https://evil.invalid/ onerror=alert(1)>",
                  url="https://example.invalid/p3", is_demo=True)
        with closing(ApplicationStore(self.db_path, demo=True)) as store:
            job_id, _, _ = store.save_job(job, SourcePolicy(source="demo"))
            self.package = {"scope": "LOCAL_DEMO", "job": job.model_dump(mode="json"),
                            "job_id": job_id, "job_hash": job.jd_hash, "profile_version": "p1",
                            "resume_version": "r1", "entry": self.origin + "/application/" + APP_ID,
                            "fields": {"name": "DEMO Alice", "email": "demo@example.invalid",
                            "summary": "DEMO Python 中文", "resume_text": "DEMO confirmed facts"}, "evidence": []}
            store.create_prepared(job_id, self.package, APP_ID)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, method="GET", path=None, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path or "/application/" + APP_ID, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def approve(self):
        from jobhunt.v2.application_store import ApplicationStore
        with closing(ApplicationStore(self.db_path, demo=True)) as store:
            store.mark_filled(APP_ID)
            store.decide(APP_ID, "approve", digest(self.package))
            return store.begin_submission(APP_ID, digest(self.package))

    def post(self, token, **changes):
        payload = {"approval_token": token, "content_digest": digest(self.package), "fields": self.package["fields"]} | changes
        return self.request("POST", body=json.dumps(payload, ensure_ascii=False).encode(),
                            headers={"Origin": self.origin, "Content-Type": "application/json"})

    def test_page_has_fixed_accessible_labels_and_escapes_untrusted_job(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        for label in ("DEMO name", "DEMO email", "DEMO summary", "DEMO resume_text", "DEMO approval token", "Validate DEMO", "Submit DEMO"):
            self.assertIn(label, body)
        self.assertIn("JobHuntAgent P3 DEMO " + APP_ID, body)
        self.assertNotIn("<img src=https://evil.invalid/", body)
        self.assertNotIn("</script><img", body)
        self.assertIn("&lt;/script&gt;&lt;img", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_only_canonical_application_routes_are_served(self):
        for path in ("/", "/application/" + APP_ID + "?x=1", "/application/" + APP_ID + "/",
                     "/application/APP-" + "b" * 32, "/../.env", "http://evil.invalid/application/" + APP_ID):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, headers={"Host": f"127.0.0.1:{self.port}"})[0], 404)

    def test_host_and_origin_are_enforced_without_cors(self):
        for headers in ({"Host": "evil.invalid"}, {"Host": f"localhost:{self.port}"},
                        {"Origin": "http://evil.invalid"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                status, response_headers, _ = self.request(headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        self.assertEqual(self.request("OPTIONS")[0], 405)

    def test_post_requires_same_origin_json_and_bounded_unique_object(self):
        valid = {"Origin": self.origin, "Content-Type": "application/json"}
        for body, headers, expected in ((b"{}", {}, 403), (b"{}", {"Origin": self.origin}, 415),
                        (b"not-json", valid, 400), (b"[]", valid, 400),
                        (b'{"fields":{},"fields":{}}', valid, 400),
                        (b"x" * (128 * 1024 + 1), valid, 413)):
            with self.subTest(expected=expected, headers=headers):
                self.assertEqual(self.request("POST", body=body, headers=headers)[0], expected)

    def test_server_refuses_unapproved_and_modified_content(self):
        self.assertEqual(self.post("unknown-token")[0], 409)
        token = self.approve()
        self.assertEqual(self.post(token, fields=self.package["fields"] | {"summary": "changed"})[0], 409)
        self.assertEqual(self.post(token, content_digest="0" * 64)[0], 409)
        self.assertEqual(self.request(path="/receipt/" + APP_ID)[0], 404)

    def test_submit_creates_durable_receipt_and_receipt_get_is_read_only(self):
        from jobhunt.v2.application_store import ApplicationStore
        token = self.approve()
        status, _, body = self.post(token)
        self.assertEqual(status, 200)
        receipt = json.loads(body)
        self.assertEqual(receipt["application_id"], APP_ID)
        self.assertEqual(receipt["content_digest"], digest(self.package))
        status, _, body = self.request(path="/receipt/" + APP_ID)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), receipt)
        with closing(ApplicationStore(self.db_path, demo=True)) as reopened:
            self.assertEqual(reopened.receipt(APP_ID), receipt)
        self.assertNotIn(token, body)

    def test_http_error_and_access_logs_never_include_payload_or_path(self):
        output = io.StringIO()
        with redirect_stderr(output):
            status, _, body = self.request("POST", body=b"secret-marker", headers={
                "Origin": self.origin, "Content-Type": "application/json"})
            self.request(path="/private-secret-marker")
        self.assertEqual(status, 400)
        self.assertNotIn("secret-marker", body)
        self.assertEqual(output.getvalue(), "")

    def test_fetch_receipt_reads_loopback_without_proxy_or_redirects(self):
        from jobhunt.v2.local_executor import fetch_receipt
        token = self.approve()
        self.assertEqual(self.post(token)[0], 200)
        receipt = fetch_receipt(self.package["entry"], APP_ID)
        self.assertEqual(receipt["content_digest"], digest(self.package))


if __name__ == "__main__":
    unittest.main()
