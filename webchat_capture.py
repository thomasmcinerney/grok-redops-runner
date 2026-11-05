#!/usr/bin/env python3
"""Capture a scoped web-chat scenario with full widget and transport evidence.

The browser adapter drives only the configured page and optional observed API
prefixes. Each lane gets a separate output directory and browser context.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone


SCHEMA = "redops-webchat-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    temp = path.with_name("." + path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def load_scope(args: argparse.Namespace) -> dict:
    if args.scope_file:
        raw = json.loads(Path(args.scope_file).read_text(encoding="utf-8"))
    else:
        url = args.redops_url.rstrip("/") + f"/api/scope/{args.engagement}"
        with urllib.request.urlopen(url, timeout=10) as response:
            raw = json.load(response)
    web = raw.get("web", raw)
    sys.path.insert(0, str(Path(args.redops_dir).resolve()))
    from redops import webscope
    return webscope.normalize_scope(web.get("pages", []), web.get("api_prefixes", []))


def check_page(url: str, scope: dict, redops_dir: str) -> None:
    sys.path.insert(0, str(Path(redops_dir).resolve()))
    from redops import webscope
    if not webscope.page_allowed(url, scope):
        raise ValueError(f"page outside exact web-chat scope: {url}")


def check_api(url: str, scope: dict, redops_dir: str) -> None:
    sys.path.insert(0, str(Path(redops_dir).resolve()))
    from redops import webscope
    if not webscope.api_allowed(url, scope):
        raise ValueError(f"API outside configured messaging prefixes: {url}")


def post_redops(url: str, engagement: int, page_url: str, event: dict, network: list) -> int:
    body = {
        "engagement_id": engagement, "page_url": page_url,
        "source_id": event["source_id"],
        "action": {"type": event["type"], "input": event["input"],
                   "url": event.get("api_url")},
        "output": event["output"], "network": network,
        "artifact_hashes": event["artifact_hashes"],
    }
    request = urllib.request.Request(
        url.rstrip("/") + "/api/webchat/capture",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return int(json.load(response)["id"])


async def settle(page, frame, wait_ms: int) -> str:
    await page.wait_for_timeout(wait_ms)
    try:
        return await frame.locator("body").inner_text(timeout=10000)
    except Exception as exc:
        return f"[widget text unavailable: {exc}]"


async def record(args: argparse.Namespace) -> dict:
    from playwright.async_api import async_playwright

    os.umask(0o077)
    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    page_url = str(scenario.get("page_url") or "")
    actions = scenario.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("scenario needs at least one action")
    scope = load_scope(args)
    check_page(page_url, scope, args.redops_dir)
    run_dir = Path(args.out).expanduser().resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("output directory already contains files")
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    (run_dir / "screenshots").mkdir(mode=0o700)
    (run_dir / "network").mkdir(mode=0o700)
    (run_dir / "uploads").mkdir(mode=0o700)
    manifest = {
        "schema": SCHEMA, "run_id": str(uuid.uuid4()),
        "page_url": page_url, "scope": scope, "started_at": now(),
        "events": [], "status": "running",
    }
    write_json(run_dir / "manifest.json", manifest)
    traffic: list[dict] = []
    tasks: list[asyncio.Task] = []
    nav_errors: list[str] = []

    async def request_event(req):
        try:
            body = req.post_data_buffer or b""
            traffic.append({
                "at": now(), "type": "request", "url": req.url,
                "method": req.method, "resource_type": req.resource_type,
                "headers": await req.all_headers(),
                "body_b64": base64.b64encode(body).decode("ascii"),
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            })
        except Exception as exc:
            traffic.append({"at": now(), "type": "request_error", "url": req.url,
                            "error": str(exc)})

    async def response_event(resp):
        try:
            body = await resp.body()
            traffic.append({
                "at": now(), "type": "response", "url": resp.url,
                "status": resp.status, "resource_type": resp.request.resource_type,
                "headers": await resp.all_headers(),
                "body_b64": (base64.b64encode(body).decode("ascii")
                             if resp.request.resource_type in {"xhr", "fetch", "document"}
                             else None),
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            })
        except Exception as exc:
            traffic.append({"at": now(), "type": "response_error", "url": resp.url,
                            "error": str(exc)})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=args.browser_executable, headless=not args.headed,
            args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=False,
                                             service_workers="block")
        page = await context.new_page()
        page.on("request", lambda req: tasks.append(asyncio.create_task(request_event(req))))
        page.on("response", lambda resp: tasks.append(asyncio.create_task(response_event(resp))))
        page.on("popup", lambda popup: tasks.append(asyncio.create_task(popup.close())))

        def on_navigation(frame):
            if frame == page.main_frame:
                try:
                    check_page(frame.url, scope, args.redops_dir)
                except ValueError as exc:
                    nav_errors.append(str(exc))
        page.on("framenavigated", on_navigation)

        def on_socket(socket):
            traffic.append({"at": now(), "type": "websocket_open", "url": socket.url})
            socket.on("framesent", lambda frame: traffic.append(
                {"at": now(), "type": "websocket_sent", "url": socket.url,
                 "payload": frame}))
            socket.on("framereceived", lambda frame: traffic.append(
                {"at": now(), "type": "websocket_received", "url": socket.url,
                 "payload": frame}))
        page.on("websocket", on_socket)

        current_action = None
        action_traffic_start = 0
        try:
            for index, action in enumerate(actions, 1):
                current_action = action
                action_traffic_start = len(traffic)
                kind = str(action.get("type") or "")
                if kind not in {"open", "button", "message", "upload", "api"}:
                    raise ValueError(f"unsupported action {kind!r}")
                start = len(traffic)
                aid = f"{index:03d}"
                captured_input = action.get("text") if kind in {"button", "message"} else action
                if kind == "open":
                    navigation = await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                    check_page(page.url, scope, args.redops_dir)
                    launcher = page.frame_locator('iframe#launcher').locator("button")
                    if navigation and navigation.status >= 400:
                        if not args.headed:
                            raise RuntimeError(f"page returned HTTP {navigation.status}")
                        # Let the operator complete interactive verification in the visible browser.
                        await launcher.wait_for(timeout=120000)
                        check_page(page.url, scope, args.redops_dir)
                    await launcher.click(timeout=20000)
                elif kind == "button":
                    frame = page.frame_locator('iframe[title="Messaging window"]')
                    await frame.get_by_role("button", name=str(action["text"]), exact=True).click(
                        timeout=20000)
                elif kind == "message":
                    frame = page.frame_locator('iframe[title="Messaging window"]')
                    field = frame.locator("#composer-input")
                    await field.fill(str(action["text"]), timeout=20000)
                    await field.press("Enter")
                elif kind == "upload":
                    frame = page.frame_locator('iframe[title="Messaging window"]')
                    source = Path(str(action["file"])).expanduser().resolve()
                    if not source.is_file():
                        raise ValueError(f"upload file missing: {source}")
                    copied = run_dir / "uploads" / f"{aid}-{source.name}"
                    shutil.copy2(source, copied)
                    await frame.locator('input[type="file"]').set_input_files(str(copied))
                    captured_input = {"file": str(copied.relative_to(run_dir)),
                                      "sha256": sha(copied)}
                elif kind == "api":
                    check_api(str(action["url"]), scope, args.redops_dir)
                    method = str(action.get("method") or "GET").upper()
                    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                        raise ValueError("unsupported API method")
                    body = action.get("body")
                    raw = body.encode("utf-8") if isinstance(body, str) else (
                        json.dumps(body).encode("utf-8") if body is not None else None)
                    response = await context.request.fetch(
                        str(action["url"]), method=method,
                        headers=action.get("headers") or {}, data=raw, timeout=30000)
                    api_body = await response.body()
                    captured_input = {"method": method, "url": action["url"],
                                      "headers": action.get("headers") or {},
                                      "body": body}
                if nav_errors:
                    raise ValueError(nav_errors[-1])
                frame = page.frame_locator('iframe[title="Messaging window"]')
                widget_text = await settle(page, frame, int(action.get("wait_ms", args.wait_ms)))
                if kind == "api":
                    output = json.dumps({"status": response.status,
                                         "headers": await response.all_headers(),
                                         "body_b64": base64.b64encode(api_body).decode("ascii"),
                                         "widget_text": widget_text}, ensure_ascii=False)
                else:
                    output = widget_text
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                selected = traffic[start:]
                network_path = run_dir / "network" / f"{aid}.jsonl"
                network_path.write_text(
                    "".join(json.dumps(x, ensure_ascii=False, default=str) + "\n"
                            for x in selected), encoding="utf-8")
                screenshot_path = run_dir / "screenshots" / f"{aid}.png"
                await page.screenshot(path=str(screenshot_path))
                event = {
                    "id": aid, "source_id": f"{manifest['run_id']}:{aid}",
                    "at": now(), "type": kind, "input": captured_input,
                    "output": output,
                    "network_path": str(network_path.relative_to(run_dir)),
                    "screenshot_path": str(screenshot_path.relative_to(run_dir)),
                    "artifact_hashes": {"network": sha(network_path),
                                        "screenshot": sha(screenshot_path)},
                }
                if kind == "api":
                    event["api_url"] = str(action["url"])
                manifest["events"].append(event)
                write_json(run_dir / "manifest.json", manifest)
                if args.engagement is not None:
                    event["redops_event_id"] = post_redops(
                        args.redops_url, args.engagement, page_url, event, selected)
                    write_json(run_dir / "manifest.json", manifest)
            manifest["status"] = "completed"
        except BaseException as exc:
            manifest["status"] = "failed"
            manifest["error"] = repr(exc)
            if current_action is not None:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                network_path = run_dir / "network" / "failure.jsonl"
                network_path.write_text(
                    "".join(json.dumps(x, ensure_ascii=False, default=str) + "\n"
                            for x in traffic[action_traffic_start:]), encoding="utf-8")
                screenshot_path = run_dir / "screenshots" / "failure.png"
                try:
                    await page.screenshot(path=str(screenshot_path), timeout=10000)
                    screenshot_hash = sha(screenshot_path)
                except Exception:
                    screenshot_hash = None
                try:
                    page_text = await page.locator("body").inner_text(timeout=5000)
                except Exception:
                    page_text = ""
                manifest["failure"] = {
                    "at": now(), "input": current_action,
                    "output": {"error": str(exc), "page_url": page.url,
                               "page_text": page_text},
                    "network_path": str(network_path.relative_to(run_dir)),
                    "screenshot_path": (str(screenshot_path.relative_to(run_dir))
                                        if screenshot_hash else None),
                    "artifact_hashes": {"network": sha(network_path),
                                        "screenshot": screenshot_hash},
                }
            raise
        finally:
            manifest["finished_at"] = now()
            write_json(run_dir / "manifest.json", manifest)
            await context.close()
            await browser.close()
    if args.report_case:
        sys.path.insert(0, str(Path(args.report_module_dir).resolve()))
        from webchat_import import import_run
        manifest["report_event_ids"] = import_run(
            Path(args.report_case).resolve(), args.target, args.stage,
            run_dir / "manifest.json")
        write_json(run_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="JSON page URL and actions")
    parser.add_argument("--out", required=True, help="new private lane directory")
    parser.add_argument("--redops-dir", required=True, help="RedOps source directory")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--scope-file", help="RedOps scope_show JSON or web scope JSON")
    scope.add_argument("--engagement", type=int, help="fetch scope and post results to RedOps")
    parser.add_argument("--redops-url", default="http://127.0.0.1:9100")
    parser.add_argument("--browser-executable", default="/usr/bin/chromium")
    parser.add_argument("--wait-ms", type=int, default=2500)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--report-case")
    parser.add_argument("--report-module-dir")
    parser.add_argument("--target")
    parser.add_argument("--stage")
    args = parser.parse_args()
    if args.report_case and not all((args.report_module_dir, args.target, args.stage)):
        parser.error("report import needs --report-module-dir, --target, and --stage")
    if args.wait_ms < 0:
        parser.error("--wait-ms must be nonnegative")
    try:
        result = asyncio.run(record(args))
    except Exception as exc:
        print(f"webchat capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                      "events": len(result["events"]),
                      "manifest": str(Path(args.out).resolve() / "manifest.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
