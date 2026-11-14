"""Bounded loopback-only P3 fixture; the business store owns submission authority."""
from contextlib import closing
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets

from ..config import UserError
from ..models import digest
from .application_writer import MAX_SUBMISSION_BYTES, validate_fields

FIELDS = {key: "DEMO " + key for key in ("name", "email", "summary", "resume_text")}
TOKEN_LABEL = "DEMO approval token"
EXPECTED = {("Edit", name) for name in FIELDS.values()} | {
    ("Edit", TOKEN_LABEL), ("Button", "Validate DEMO"), ("Button", "Submit DEMO")}
MAX_BODY = MAX_SUBMISSION_BYTES
APP_PATTERN = r"APP-[a-f0-9]{32}"


def validate_entry(entry, application_id):
    if not isinstance(application_id, str) or not re.fullmatch(APP_PATTERN, application_id):
        raise UserError("P3_LOCAL_DEMO_URL_REQUIRED")
    matched = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/application/(" + APP_PATTERN + r")",
                           entry if isinstance(entry, str) else "")
    if matched is None or int(matched[1]) > 65535 or matched[2] != application_id:
        raise UserError("P3_LOCAL_DEMO_URL_REQUIRED")
    return int(matched[1])


def validate_package(package, application_id):
    if not isinstance(package, dict) or package.get("scope") != "LOCAL_DEMO":
        raise UserError("P3_DEMO_PACKAGE_REQUIRED")
    job, fields = package.get("job"), package.get("fields")
    if (not isinstance(job, dict) or job.get("is_demo") is not True or not isinstance(fields, dict)
            or set(fields) != set(FIELDS)
            or any(not isinstance(value, str) or not value.strip() or len(value) > 50000 or "\x00" in value
                   for value in fields.values())):
        raise UserError("P3_DEMO_PACKAGE_REQUIRED")
    validate_fields(fields)
    return validate_entry(package.get("entry"), application_id)


def _script_json(value):
    # A JSON string inside HTML must not be able to terminate its script element.
    return (json.dumps(value, ensure_ascii=True).replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("&", "\\u0026"))


def _render(package, application_id, content_digest, nonce, receipt=None):
    title = "JobHuntAgent P3 DEMO " + application_id
    job = package["job"]
    controls = []
    for key, label in FIELDS.items():
        if key in {"summary", "resume_text"}:
            control = f'<textarea id="{key}" aria-label="{label}" rows="4" maxlength="50000"></textarea>'
        else:
            control = f'<input id="{key}" aria-label="{label}" type="text" maxlength="50000">'
        controls.append(f'<label for="{key}">{label}</label>{control}')
    feedback = "P3 receipt " + receipt["receipt_id"] if receipt else "P3 ready for review"
    markup = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style nonce="__NONCE__">body{font:16px system-ui;max-width:900px;margin:32px auto;padding:0 20px}
label{display:block;margin-top:14px}input,textarea{box-sizing:border-box;width:100%;font:inherit;padding:8px}
button{margin:18px 12px 8px 0;padding:10px 18px}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style></head><body><h1>Local DEMO application</h1>
<p>__COMPANY__ — __JOB__</p><pre>__JD__</pre>
<div role="group" aria-label="P3 package __DIGEST_TEXT__"><p>Local review package</p></div>
<form autocomplete="off">__CONTROLS__
<label for="approval_token">DEMO approval token</label>
<input id="approval_token" aria-label="DEMO approval token" type="text" autocomplete="off" spellcheck="false" maxlength="512">
<button id="validate" type="button">Validate DEMO</button>
<button id="submit" type="button">Submit DEMO</button>
</form><div role="group" aria-label="P3 feedback"><p id="feedback" role="status">__FEEDBACK__</p></div>
<script nonce="__NONCE__">
"use strict";
const applicationId = __APP_JSON__, contentDigest = __DIGEST_JSON__;
const fieldKeys = ["name", "email", "summary", "resume_text"];
const feedback = document.getElementById("feedback");
const submitButton = document.getElementById("submit");
let attempted = __ATTEMPTED__;
submitButton.disabled = attempted;
function fields() { return Object.fromEntries(fieldKeys.map(key => [key, document.getElementById(key).value])); }
function validate() {
  const values = fields();
  const valid = fieldKeys.every(key => values[key].trim().length > 0) && /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(values.email);
  feedback.textContent = valid ? "P3 validation OK" : "P3 validation failed";
  return valid;
}
document.querySelector("form").addEventListener("submit", event => event.preventDefault());
document.getElementById("validate").addEventListener("click", validate);
for (const key of fieldKeys) document.getElementById(key).addEventListener("input", () => {
  if (!attempted) feedback.textContent = "P3 fields changed";
});
submitButton.addEventListener("click", async () => {
  if (attempted || !validate()) return;
  const tokenControl = document.getElementById("approval_token");
  const approvalToken = tokenControl.value;
  if (!approvalToken) { feedback.textContent = "P3 approval required"; return; }
  attempted = true;
  submitButton.disabled = true;
  const payload = {approval_token: approvalToken, content_digest: contentDigest, fields: fields()};
  tokenControl.value = "";
  feedback.textContent = "P3 submitting";
  try {
    const response = await fetch("/application/" + applicationId, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
      credentials: "omit", redirect: "error", cache: "no-store"
    });
    if (!response.ok) throw new Error("rejected");
    const receipt = await response.json();
    if (receipt.application_id !== applicationId || receipt.content_digest !== contentDigest ||
        typeof receipt.receipt_id !== "string") throw new Error("mismatch");
    feedback.textContent = "P3 receipt " + receipt.receipt_id;
  } catch (_) { feedback.textContent = "P3 submission unconfirmed"; }
});
</script></body></html>"""
    replacements = {"__TITLE__": html.escape(title), "__NONCE__": nonce,
                    "__COMPANY__": html.escape(str(job.get("company", ""))),
                    "__JOB__": html.escape(str(job.get("title", ""))),
                    "__JD__": html.escape(str(job.get("jd", ""))),
                    "__CONTROLS__": "".join(controls), "__FEEDBACK__": html.escape(feedback),
                    "__APP_JSON__": _script_json(application_id), "__DIGEST_JSON__": _script_json(content_digest),
                    "__DIGEST_TEXT__": html.escape(content_digest),
                    "__ATTEMPTED__": "true" if receipt else "false"}
    # One substitution pass: untrusted JD text cannot inject later template markers.
    return re.sub("|".join(map(re.escape, replacements)), lambda match: replacements[match[0]], markup)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class _DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Base HTTP error logging can include request text or private exception data.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "JobHuntAgentP3"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        pass

    def _respond(self, status, value, *, nonce=None):
        body = value.encode("utf-8") if nonce else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if nonce else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        policy = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        if nonce:
            policy += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._respond(code, {"error": "P3_HTTP_REQUEST_REJECTED"})

    def _guard(self, *, post=False):
        expected_host = f"127.0.0.1:{self.server.server_address[1]}"
        origins = self.headers.get_all("Origin", [])
        if (self.headers.get_all("Host", []) != [expected_host]
                or len(origins) > 1 or (origins and origins != ["http://" + expected_host])
                or (post and not origins)):
            self._respond(403, {"error": "P3_ORIGIN_REJECTED"})
            return False
        return True

    def _route(self, kind):
        matched = re.fullmatch("/" + kind + "/(" + APP_PATTERN + ")", self.path)
        if not matched:
            self._respond(404, {"error": "P3_NOT_FOUND"})
            return None
        return matched[1]

    def _store(self):
        from .application_store import ApplicationStore
        return ApplicationStore(self.server.database_path, demo=True)

    def _package(self, store, identifier):
        package = store.package(identifier)
        port = validate_package(package, identifier)
        if port != self.server.server_address[1]:
            raise UserError("P3_LOCAL_DEMO_URL_REQUIRED")
        row = store.get_application(identifier)
        if row["content_digest"] != digest(package):
            raise UserError("P3_PACKAGE_DIGEST_MISMATCH")
        return package, row["content_digest"]

    def do_GET(self):
        if not self._guard():
            return
        kind = "receipt" if self.path.startswith("/receipt/") else "application"
        identifier = self._route(kind)
        if not identifier:
            return
        try:
            with closing(self._store()) as store:
                package, content_digest = self._package(store, identifier)
                receipt = store.receipt(identifier)
                if kind == "receipt":
                    self._respond(200, receipt) if receipt else self._respond(404, {"error": "P3_RECEIPT_NOT_FOUND"})
                else:
                    nonce = secrets.token_hex(16)
                    self._respond(200, _render(package, identifier, content_digest, nonce, receipt), nonce=nonce)
        except UserError:
            self._respond(404, {"error": "P3_NOT_FOUND"})
        except Exception:
            self._respond(500, {"error": "P3_SERVICE_FAILED"})

    def do_POST(self):
        if not self._guard(post=True):
            return
        identifier = self._route("application")
        if not identifier:
            return
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].lower() not in {"application/json", "application/json; charset=utf-8"}:
            self._respond(415, {"error": "P3_JSON_REQUIRED"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self._respond(400, {"error": "P3_BODY_INVALID"})
            return
        length = int(lengths[0])
        if length > MAX_BODY:
            self._respond(413, {"error": "P3_BODY_TOO_LARGE"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"), object_pairs_hook=_unique_object)
            if (not isinstance(payload, dict) or set(payload) != {"approval_token", "content_digest", "fields"}
                    or not isinstance(payload["approval_token"], str) or not 1 <= len(payload["approval_token"]) <= 512
                    or not isinstance(payload["content_digest"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", payload["content_digest"])
                    or not isinstance(payload["fields"], dict) or set(payload["fields"]) != set(FIELDS)
                    or any(not isinstance(value, str) for value in payload["fields"].values())):
                raise ValueError()
        except (ValueError, UnicodeError, OSError):
            self._respond(400, {"error": "P3_BODY_INVALID"})
            return
        try:
            with closing(self._store()) as store:
                self._package(store, identifier)
                receipt = store.accept_submission(identifier, payload["approval_token"], payload["content_digest"], payload["fields"])
            self._respond(200, receipt)
        except UserError:
            self._respond(409, {"error": "P3_SUBMISSION_REJECTED"})
        except Exception:
            self._respond(500, {"error": "P3_SERVICE_FAILED"})

    def do_OPTIONS(self):
        self._respond(405, {"error": "P3_METHOD_NOT_ALLOWED"})


def create_server(home: Path, port: int):
    if type(port) is not int or not 0 <= port <= 65535:
        raise UserError("P3_LOCAL_PORT_INVALID")
    server = _DemoServer(("127.0.0.1", port), _Handler)
    server.database_path = Path(home).resolve() / "data" / "v2" / "demo" / "business.sqlite3"
    return server


def serve(home: Path, port: int):
    with create_server(home, port) as server:
        server.serve_forever()
