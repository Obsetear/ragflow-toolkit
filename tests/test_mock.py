#!/usr/bin/env python3
"""端到端测试：用一个 mock RAGFlow 服务器验证批量导入全流程。

不依赖真实 RAGFlow，不需要网络。运行：
  python tests/test_mock.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 把包目录加进 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RECEIVED = []          # (dataset_id, filename)
PARSED = []            # dataset_id


class MockRF(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/v1/datasets"):
            self._json({"code": 0, "data": [], "total": 0})
        else:
            self._json({"code": 404}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if self.path == "/api/v1/datasets":
            self._json({"code": 0, "data": {"id": "ds_mock"}})
        elif self.path.endswith("/documents"):
            ds = self.path.split("/")[4]
            # 从 multipart 里抠出 filename
            fn = ""
            for line in body.split(b"\r\n"):
                if b'name="file"' in line and b"filename=" in line:
                    fn = line.split(b'filename="')[1].rsplit(b'"', 1)[0].decode()
            RECEIVED.append((ds, fn))
            self._json({"code": 0, "data": [{"id": f"doc{len(RECEIVED)}"}]})
        elif self.path.endswith("/chunks"):
            PARSED.append(self.path.split("/")[4])
            self._json({"code": 0})
        else:
            self._json({"code": 404}, 404)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        (src / "子目录").mkdir(parents=True)
        (src / "a.md").write_text("# a", encoding="utf-8")
        (src / "b.md").write_text("# b", encoding="utf-8")
        (src / "子目录" / "c.md").write_text("# c", encoding="utf-8")
        (src / "skip.pdf").write_text("x", encoding="utf-8")

        os.environ["RAGFLOW_TOOLKIT_STATE"] = str(tmp / "state.db")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockRF)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        # 以子进程方式跑 CLI，模拟真实使用
        import subprocess
        env = dict(os.environ)
        env["RAGFLOW_TOOLKIT_STATE"] = str(tmp / "state.db")
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "ragflow_toolkit.py"),
             "upload", "--base-url", f"http://127.0.0.1:{port}", "--api-key", "test",
             "--source", str(src), "--dataset", "测试库", "--workers", "2", "--ext", ".md"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )

        print("STDOUT:", r.stdout[-800:])
        if r.returncode != 0:
            print("STDERR:", r.stderr[-800:])
            print("FAIL: 退出码", r.returncode)
            srv.shutdown()
            return 1

        names = sorted(fn for _, fn in RECEIVED)
        assert len(RECEIVED) == 3, f"应上传 3 个 md，实际 {len(RECEIVED)}: {names}"
        assert "a.md" in names and "b.md" in names, names
        assert any("c.md" in n for n in names), "子目录文件未上传"
        assert not any(n.endswith(".pdf") for n in names), "不应上传 .pdf"
        assert PARSED, "解析未触发"

        # 二次运行应全部跳过（断点续传）
        r2 = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "ragflow_toolkit.py"),
             "upload", "--base-url", f"http://127.0.0.1:{port}", "--api-key", "test",
             "--source", str(src), "--dataset", "测试库", "--workers", "2", "--ext", ".md"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
        assert "skip=3" in r2.stdout, f"断点续传失效: {r2.stdout[-300:]}"

        srv.shutdown()
        print("PASS: 3 个 md 上传、子目录、扩展名过滤、解析触发、断点续传 全部通过")
        return 0


if __name__ == "__main__":
    sys.exit(main())
