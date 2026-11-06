"""Network-free end-to-end check of webchat capture against a local mock widget."""
from __future__ import annotations

import http.server
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


PAGE = b'''<!doctype html><html><body>
<a href="/signin">Sign in</a>
<iframe id="launcher" src="/launcher"></iframe>
<iframe title="Messaging window" src="/widget" style="width:400px;height:500px"></iframe>
</body></html>'''
LAUNCHER = b'<html><body><button>Open chat</button></body></html>'
SIGNIN = b'''<html><body><form method="post" action="/signin">
<label>Email<input type="email" name="email"></label>
<label>Password<input type="password" name="password"></label>
<button>Sign in</button></form></body></html>'''
WIDGET = b'''<!doctype html><html><body>
<div id="messages">Hello</div>
<button onclick="document.querySelector('#messages').textContent += ' Provider selected'">Provider</button>
<input id="composer-input" aria-label="Type a message">
<button aria-label="Send message" onclick="
const value=document.querySelector('#composer-input').value;
const line=document.createElement('div');line.textContent=value;document.body.appendChild(line);
fetch('/reply',{method:'POST',body:value}).then(r=>r.text()).then(
t=>document.querySelector('#messages').textContent+=' '+t)">Send</button>
</body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        if self.path == "/blocked":
            self.send_error(403, "Verification required")
            return
        body = {"/help": PAGE, "/launcher": LAUNCHER,
                "/widget": WIDGET, "/signin": SIGNIN}.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/signin":
            self.send_response(302)
            self.send_header("Location", "/help")
            self.end_headers()
            return
        reply = b"Echo:" + body
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


class WebchatCaptureTest(unittest.TestCase):
    def test_login_captures_auth_flow_without_plaintext_manifest_password(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root_url = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scope = root / "scope.json"
                scope.write_text(json.dumps({"pages": [root_url + "/help",
                                                       root_url + "/signin"],
                                             "api_prefixes": []}))
                credentials = root / "credentials.json"
                credentials.write_text(json.dumps({"email": "test@example.invalid",
                                                   "password": "synthetic-password"}))
                scenario = root / "scenario.json"
                scenario.write_text(json.dumps({"page_url": root_url + "/help",
                                                "actions": [
                                                    {"type": "visit", "wait_ms": 100},
                                                    {"type": "link", "text": "Sign in",
                                                     "wait_ms": 100},
                                                    {"type": "login",
                                                     "credentials_file": str(credentials),
                                                     "wait_ms": 100},
                                                    {"type": "open", "wait_ms": 100},
                                                ]}))
                out = root / "auth-lane"
                run = subprocess.run([
                    sys.executable, str(Path(__file__).with_name("webchat_capture.py")),
                    "--scenario", str(scenario), "--out", str(out),
                    "--scope-file", str(scope), "--redops-dir",
                    str(Path(__file__).parent.parent / "redops-public"),
                    "--wait-ms", "100",
                ], text=True, capture_output=True, timeout=40)
                self.assertEqual(run.returncode, 0, run.stderr)
                manifest = json.loads((out / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(len(manifest["events"]), 4)
                self.assertNotIn("synthetic-password", json.dumps(manifest))
                self.assertEqual(manifest["events"][2]["input"]["email"],
                                 "test@example.invalid")
                self.assertTrue((out / manifest["events"][2]["input"][
                    "storage_state_path"]).is_file())
                self.assertTrue((out / manifest["events"][2]["network_path"]).is_file())
        finally:
            server.shutdown()
            server.server_close()

    def test_complete_mock_exchange(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/help"
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scope = root / "scope.json"
                scope.write_text(json.dumps({"pages": [url], "api_prefixes": [
                    f"http://127.0.0.1:{server.server_port}/reply"]}))
                scenario = root / "scenario.json"
                scenario.write_text(json.dumps({"page_url": url, "actions": [
                    {"type": "open", "wait_ms": 100},
                    {"type": "button", "text": "Provider", "wait_ms": 100},
                    {"type": "message", "text": "hello", "wait_ms": 300},
                    {"type": "api", "method": "POST",
                     "url": f"http://127.0.0.1:{server.server_port}/reply",
                     "body": "test-api", "wait_ms": 100},
                ]}))
                out = root / "lane"
                run = subprocess.run([
                    sys.executable, str(Path(__file__).with_name("webchat_capture.py")),
                    "--scenario", str(scenario), "--out", str(out),
                    "--scope-file", str(scope), "--redops-dir",
                    str(Path(__file__).parent.parent / "redops-public"),
                    "--wait-ms", "100",
                ], text=True, capture_output=True, timeout=40)
                self.assertEqual(run.returncode, 0, run.stderr)
                manifest = json.loads((out / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(len(manifest["events"]), 4)
                self.assertIn("Provider selected", manifest["events"][1]["output"])
                self.assertIn("Echo:hello", manifest["events"][2]["output"])
                api = json.loads(manifest["events"][3]["output"])
                self.assertEqual(api["status"], 200)
                self.assertEqual(base64.b64decode(api["body_b64"]), b"Echo:test-api")
                for event in manifest["events"]:
                    self.assertTrue((out / event["network_path"]).is_file())
                    self.assertTrue((out / event["screenshot_path"]).is_file())
                network = (out / manifest["events"][2]["network_path"]).read_text()
                self.assertIn("/reply", network)
                self.assertIn("body_b64", network)
                api_network = (out / manifest["events"][3]["network_path"]).read_text()
                self.assertIn('"source": "direct_api"', api_network)
                self.assertIn('"type": "request"', api_network)
                self.assertIn('"type": "response"', api_network)
        finally:
            server.shutdown()
            server.server_close()

    def test_failed_page_preserves_evidence(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/blocked"
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scope = root / "scope.json"
                scope.write_text(json.dumps({"pages": [url], "api_prefixes": []}))
                scenario = root / "scenario.json"
                scenario.write_text(json.dumps({"page_url": url, "actions": [{"type": "open"}]}))
                out = root / "lane"
                report_module_dir = Path(__file__).parent.parent / "redops-public" / "report-kit"
                case = root / "case"
                init = subprocess.run([
                    sys.executable, str(report_module_dir / "report.py"), "init",
                    str(case), "--title", "Mock chatbot", "--scope", url,
                ], text=True, capture_output=True, timeout=20)
                self.assertEqual(init.returncode, 0, init.stderr)
                add_target = subprocess.run([
                    sys.executable, str(report_module_dir / "report.py"), "target",
                    str(case), "--id", "chat", "--name", "Mock chatbot",
                    "--ip", "127.0.0.1", "--role", "chatbot",
                    "--objective", "Capture a blocked page", "--summary", "Mock",
                    "--unscored",
                ], text=True, capture_output=True, timeout=20)
                self.assertEqual(add_target.returncode, 0, add_target.stderr)
                add_stage = subprocess.run([
                    sys.executable, str(report_module_dir / "report.py"), "stage",
                    str(case), "--target", "chat", "--kind", "recon",
                    "--summary", "Capture the chatbot response",
                ], text=True, capture_output=True, timeout=20)
                self.assertEqual(add_stage.returncode, 0, add_stage.stderr)
                run = subprocess.run([
                    sys.executable, str(Path(__file__).with_name("webchat_capture.py")),
                    "--scenario", str(scenario), "--out", str(out),
                    "--scope-file", str(scope), "--redops-dir",
                    str(Path(__file__).parent.parent / "redops-public"),
                    "--report-case", str(case),
                    "--report-module-dir", str(report_module_dir),
                    "--target", "chat", "--stage", "recon",
                ], text=True, capture_output=True, timeout=30)
                self.assertNotEqual(run.returncode, 0)
                manifest = json.loads((out / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(len(manifest["events"]), 1)
                self.assertEqual(manifest["events"][0]["type"], "error")
                self.assertIn("HTTP 403", manifest["events"][0]["output"])
                self.assertIn("HTTP 403", manifest["failure"]["output"]["error"])
                self.assertTrue((out / manifest["failure"]["screenshot_path"]).is_file())
                self.assertTrue((out / manifest["failure"]["network_path"]).is_file())
                self.assertIn("report_event_ids", manifest,
                              manifest.get("report_import_error"))
                self.assertEqual(len(manifest["report_event_ids"]), 1)
                case_data = json.loads((case / "case.json").read_text())
                self.assertTrue(any(e.get("source_system") == "webchat" and
                                    "HTTP 403" in e.get("result", "")
                                    for e in case_data["events"]))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
