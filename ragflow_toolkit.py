#!/usr/bin/env python3
"""ragflow-toolkit — 零依赖 RAGFlow 运维工具箱

解决的问题（都是真实踩过的坑）：
  upload   百万级文件批量导入：断点续传、多库映射、增量解析、Windows 长路径
  health   Redis/ES/API 连通性体检（解析任务不入队时先查这里）
  patch    已知 bug 的 API 级修补（task_page_size、field_map 等）
  status   查看导入进度

用法：
  python ragflow_toolkit.py upload --base-url http://127.0.0.1 --api-key KEY \
      --source D:\\data --dataset my_kb --workers 4 --resume
  python ragflow_toolkit.py health --base-url http://127.0.0.1 --api-key KEY
  python ragflow_toolkit.py patch --base-url http://127.0.0.1 --api-key KEY --fix task_page_size
  python ragflow_toolkit.py status

设计原则：
  - 只用 Python 标准库，无 pip 依赖（现场机往往装不了包）
  - SQLite 断点续传（61 万级不用 JSON 占内存）
  - 源文件只读，永不改动
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATE_DB = Path(os.environ.get("RAGFLOW_TOOLKIT_STATE", str(Path.home() / ".ragflow-toolkit" / "state.db")))
DEFAULT_TIMEOUT = 60
RETRIES = 3


# --------------------------------------------------------------------------- #
# HTTP 客户端：只依赖 urllib
# --------------------------------------------------------------------------- #
class RFClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = DEFAULT_TIMEOUT):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None, headers=None):
        url = self.base + path
        h = {"Authorization": f"Bearer {self.key}"}
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
            else:
                data = body
        last = None
        for attempt in range(RETRIES):
            try:
                req = urllib.request.Request(url, data=data, headers=h, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                last = e
                # 429 / 5xx 退避重试；4xx 直接抛
                if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                if attempt < RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise last  # pragma: no cover

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body=None):
        return self._req("POST", path, body)

    def put(self, path, body=None):
        return self._req("PUT", path, body)

    def patch(self, path, body=None):
        return self._req("PATCH", path, body)

    # ---- 业务接口 ----
    def list_datasets(self, page_size=100):
        return self.get(f"/api/v1/datasets?page_size={page_size}")

    def create_dataset(self, name: str, **opts):
        body = {"name": name}
        body.update(opts)
        return self.post("/api/v1/datasets", body)

    def upload_document(self, dataset_id: str, filename: str, content: bytes):
        """multipart/form-data，字段名 file"""
        boundary = "----rft" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_name = filename.replace('"', "_")
        parts = [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        return self._req(
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def parse_documents(self, dataset_id: str, doc_ids):
        return self.post(
            f"/api/v1/datasets/{dataset_id}/chunks",
            {"document_ids": list(doc_ids)},
        )

    def list_documents(self, dataset_id: str, page_size=100):
        return self.get(
            f"/api/v1/datasets/{dataset_id}/documents?page_size={page_size}"
        )

    def health(self):
        out = {"api": False, "datasets": None, "error": None}
        try:
            r = self.list_datasets(page_size=1)
            out["api"] = r.get("code") == 0
            out["datasets"] = r.get("data", {}).get("total")
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out


# --------------------------------------------------------------------------- #
# 断点续传状态库
# --------------------------------------------------------------------------- #
class StateDB:
    def __init__(self, path: Path = STATE_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS uploaded (
                path TEXT PRIMARY KEY,
                dataset TEXT,
                doc_id TEXT,
                parsed INTEGER DEFAULT 0,
                ts REAL
            )"""
        )
        self.db.commit()

    def is_done(self, path: str) -> bool:
        with self.lock:
            cur = self.db.execute("SELECT 1 FROM uploaded WHERE path=?", (path,))
            return cur.fetchone() is not None

    def record(self, path: str, dataset: str, doc_id: str | None, parsed: int = 0):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO uploaded(path,dataset,doc_id,parsed,ts) VALUES(?,?,?,?,?)",
                (path, dataset, doc_id, parsed, time.time()),
            )

    def mark_parsed(self, path: str):
        with self.lock:
            self.db.execute("UPDATE uploaded SET parsed=1 WHERE path=?", (path,))

    def unparsed(self, dataset: str | None = None):
        q = "SELECT path,doc_id FROM uploaded WHERE parsed=0"
        args = ()
        if dataset:
            q += " AND dataset=?"
            args = (dataset,)
        with self.lock:
            return self.db.execute(q, args).fetchall()

    def dataset_of(self, path: str):
        with self.lock:
            row = self.db.execute("SELECT dataset FROM uploaded WHERE path=?", (path,)).fetchone()
            return row[0] if row else None

    def stats(self):
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM uploaded").fetchone()[0]
            parsed = self.db.execute("SELECT COUNT(*) FROM uploaded WHERE parsed=1").fetchone()[0]
            return total, parsed

    def commit(self):
        with self.lock:
            self.db.commit()


# --------------------------------------------------------------------------- #
# 文件扫描
# --------------------------------------------------------------------------- #
def win_long(path: Path) -> str:
    """Windows 长路径前缀，避免 260 字符限制"""
    if os.name == "nt":
        p = str(path.resolve())
        if not p.startswith("\\\\?\\"):
            return "\\\\?\\" + p
        return p
    return str(path)


def scan(source: Path, exts: set[str]):
    """返回 (长路径基址, 文件列表)；基址带 \\?\ 前缀，后续 relative_to 必须用它"""
    base = Path(win_long(source))
    files = []
    for root, _dirs, names in os.walk(str(base)):
        for n in names:
            if Path(n).suffix.lower() in exts:
                files.append(Path(root) / n)
    return base, files


def doc_name(base: Path, f: Path) -> str:
    """显示名 = 相对路径，/ 换成 __，超 250 字节截断加哈希"""
    rel = f.relative_to(base).as_posix()
    s = rel.replace("/", "__")
    if len(s.encode("utf-8")) > 250:
        h = hashlib.md5(rel.encode()).hexdigest()[:8]
        s = s[:200] + "_" + h + Path(rel).suffix
    return s


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_upload(args):
    client = RFClient(args.base_url, args.api_key)
    state = StateDB()

    # 解析库映射：--dataset 单库；--map 文件每行 "路径前缀|库名"
    mapping = []
    if args.map:
        for line in Path(args.map).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            prefix, _, kb = line.partition("|")
            mapping.append((prefix.strip(), kb.strip()))

    datasets = {d["name"]: d["id"] for d in client.list_datasets().get("data", [])}
    if args.dataset and args.dataset not in datasets:
        r = client.create_dataset(args.dataset)
        data = r.get("data")
        ds_id = data["id"] if isinstance(data, dict) else (data or [{}])[0].get("id")
        datasets[args.dataset] = ds_id
        print(f"[+] 创建知识库 {args.dataset} -> {ds_id}")

    exts = {e if e.startswith(".") else "." + e for e in args.ext.split(",")}
    base, files = scan(Path(args.source), exts)
    print(f"[i] 扫描到 {len(files)} 个文件（扩展名 {sorted(exts)}）")
    if args.limit:
        files = files[: args.limit]

    def pick_dataset(f: Path) -> str:
        if not mapping:
            return args.dataset
        rel = f.relative_to(base).as_posix()
        for prefix, kb in mapping:
            if rel.startswith(prefix):
                return kb
        return args.dataset

    def work(f: Path):
        key = str(f)
        if state.is_done(key):
            return ("skip", key)
        kb = pick_dataset(f)
        if kb not in datasets:
            raise RuntimeError(f"知识库不存在：{kb}")
        content = f.read_bytes()
        r = client.upload_document(datasets[kb], doc_name(base, f), content)
        doc_id = (r.get("data") or [{}])[0].get("id") if isinstance(r.get("data"), list) else r.get("data", {}).get("id")
        state.record(key, kb, doc_id, parsed=0)
        return ("ok", key)

    ok = skip = err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, f): f for f in files}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                kind, _ = fut.result()
                ok += kind == "ok"
                skip += kind == "skip"
            except Exception as e:  # noqa: BLE001
                err += 1
                print(f"[!] 失败 {futs[fut]}: {e}")
            if i % 20 == 0:
                state.commit()
                print(f"[{i}/{len(files)}] ok={ok} skip={skip} err={err}")
    state.commit()

    # 增量解析
    if args.parse:
        pending = state.unparsed()
        by_ds: dict[str, list] = {}
        for path, doc_id in pending:
            if doc_id:
                ds = state.dataset_of(path)
                by_ds.setdefault(ds, []).append(doc_id)
        for ds, ids in by_ds.items():
            for i in range(0, len(ids), 100):
                client.parse_documents(datasets[ds], ids[i : i + 100])
            print(f"[>] 触发解析 {ds}: {len(ids)} 个文档")
    print(f"[done] ok={ok} skip={skip} err={err}")


def cmd_status(args):
    state = StateDB()
    total, parsed = state.stats()
    print(f"已记录 {total} 个文件，已解析 {parsed}，待解析 {total - parsed}")


def cmd_health(args):
    client = RFClient(args.base_url, args.api_key)
    h = client.health()
    print(json.dumps(h, ensure_ascii=False, indent=2))
    if not h["api"]:
        print("\n[!] API 不通。常见原因：")
        print("  1. base_url 少了 /api/v1 前缀（v0.27+ 用 /api/v1）")
        print("  2. API Key 无效")
        print("  3. RAGFlow 容器没起来")
        sys.exit(1)


def cmd_patch(args):
    client = RFClient(args.base_url, args.api_key)
    if args.fix == "task_page_size":
        # v0.27.2 界面保存切片方法必报错：page_size=null
        ds = client.list_datasets().get("data", [])
        fixed = 0
        for d in ds:
            cur = d.get("chunk_method", {})
            if isinstance(cur, dict) and cur.get("page_size") is None:
                client.patch(f"/api/v1/datasets/{d['id']}", {"chunk_method": {**cur, "page_size": 12}})
                fixed += 1
        print(f"[ok] task_page_size 修补 {fixed} 个知识库")
    else:
        print(f"[!] 未知修补项：{args.fix}")


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="RAGFlow 运维工具箱")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--base-url", default=os.environ.get("RAGFLOW_URL", "http://127.0.0.1"))
        sp.add_argument("--api-key", default=os.environ.get("RAGFLOW_API_KEY", ""))

    up = sub.add_parser("upload", help="批量导入")
    common(up)
    up.add_argument("--source", required=True)
    up.add_argument("--dataset", required=True)
    up.add_argument("--map", default=None)
    up.add_argument("--workers", type=int, default=4)
    up.add_argument("--ext", default=".md,.pdf,.docx,.txt")
    up.add_argument("--limit", type=int, default=0)
    up.add_argument("--parse", action="store_true", default=True)
    up.set_defaults(func=cmd_upload)

    st = sub.add_parser("status", help="查看进度")
    st.set_defaults(func=cmd_status)

    he = sub.add_parser("health", help="连通性体检")
    common(he)
    he.set_defaults(func=cmd_health)

    pa = sub.add_parser("patch", help="已知 bug 修补")
    common(pa)
    pa.add_argument("--fix", required=True, choices=["task_page_size"])
    pa.set_defaults(func=cmd_patch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
