#!/usr/bin/env python3
"""ragflow-monitor 的端到端测试：mock 一个带各类文档状态的知识库。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

DOCS = [
    {"id": "d1", "run": "DONE"},
    {"id": "d2", "run": "DONE"},
    {"id": "d3", "run": "FAIL"},
    {"id": "d4", "status": 4},     # FAIL
    {"id": "d5", "status": 1},     # RUNNING
]


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/v1/datasets") and "documents" not in self.path:
            self._json({"code": 0, "data": [{"id": "ds1", "name": "库A"}], "total": 1})
        elif "documents" in self.path:
            self._json({"code": 0, "data": {"total": len(DOCS), "docs": DOCS}})
        else:
            self._json({"code": 404})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, str(HERE / "ragflow_monitor.py"), "check",
         "--base-url", f"http://127.0.0.1:{port}", "--api-key", "t",
         "--json", "--max-fail-rate", "0.05"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    srv.shutdown()

    if r.returncode != 0:
        print("STDERR:", r.stderr[-800:])
        print("FAIL: 退出码", r.returncode)
        return 1

    rep = json.loads(r.stdout)
    assert rep["api_ok"] is True
    assert len(rep["datasets"]) == 1
    it = rep["datasets"][0]
    assert it["total"] == 5
    assert it["by_status"].get("已完成") == 2, it["by_status"]
    assert it["by_status"].get("失败") == 2, it["by_status"]
    assert it["by_status"].get("解析中") == 1, it["by_status"]
    # 失败率 2/4 = 50% > 5%，应告警
    assert any("失败率" in a for a in rep["alerts"]), rep["alerts"]
    print("PASS: 巡检统计各状态、失败率告警 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
