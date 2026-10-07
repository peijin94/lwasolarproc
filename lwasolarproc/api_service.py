"""Small HTTP service for realtime QA and cached fine-channel images."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .api_store import DEFAULT_IMAGE_DB, DEFAULT_QA_DB, parse_timestamp, query_images, query_qa


def make_server(qa_db: Path, image_db: Path, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                url = urlsplit(self.path)
                params = parse_qs(url.query, keep_blank_values=True)
                if set(params) - {"timestamp"} or any(len(values) != 1 for values in params.values()):
                    raise ValueError("Only one timestamp parameter is accepted")
                requested = params.get("timestamp", [None])[0]
                if requested is not None and requested != "newest":
                    parse_timestamp(requested)
                if url.path == "/health":
                    self.send_json(200, {"status": "ok", "qa_db_exists": qa_db.is_file(),
                                         "image_db_exists": image_db.is_file()})
                    return
                if url.path in {"/flagging", "/flux"}:
                    result = query_qa(qa_db, url.path[1:], requested)
                elif url.path == "/images":
                    result = query_images(image_db, requested)
                    if result:
                        query = urlencode({"timestamp": result["timestamp"]})
                        for row in result["images"]:
                            for fmt in ("fits", "npz"):
                                row[fmt + "_url"] = f"/images/{row['target_mhz']}.{fmt}?{query}"
                else:
                    match = re.fullmatch(r"/images/(30|45|60|75)\.(fits|npz)", url.path)
                    if not match:
                        self.send_json(404, {"error": "Unknown endpoint"})
                        return
                    target, fmt = int(match[1]), match[2]
                    result = query_images(image_db, requested, target_mhz=target, fmt=fmt)
                    if result:
                        content_type = "application/octet-stream" if fmt == "npz" else "application/fits"
                        self.send_bytes(200, result["data"], content_type,
                                        {"X-Observation-Timestamp": result["timestamp"],
                                         "X-Frequency-MHz": str(result["freq_mhz"])})
                        return
                if result is None:
                    self.send_json(404, {"error": "No matching data available"})
                else:
                    self.send_json(200, result)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except sqlite3.Error:
                logging.exception("API database unavailable")
                self.send_json(503, {"error": "Database unavailable"})

        def send_bytes(self, status: int, body: bytes, content_type: str,
                       headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, result: dict) -> None:
            self.send_bytes(status, json.dumps(result, allow_nan=False).encode(), "application/json")

        def log_message(self, fmt: str, *args) -> None:
            logging.debug(fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--qa-db", type=Path, default=DEFAULT_QA_DB)
    parser.add_argument("--image-db", type=Path, default=DEFAULT_IMAGE_DB)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    server = make_server(args.qa_db, args.image_db, args.host, args.port)
    logging.info("Realtime API listening on http://%s:%d", *server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
