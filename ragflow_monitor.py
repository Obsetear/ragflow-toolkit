#!/usr/bin/env python3
"""ragflow-monitor — RAGFlow 巡检/监控

运维托管服务的核心工具：一键体检、出报告、失败告警。
零依赖，与 ragflow_toolkit 共用 RFClient。

用法：
  # 一次性报告（markdown）
  python ragflow_monitor.py check --base-url http://10.0.0.10:9380 --api-key KEY

  # 深度扫描（全量文档，统计各状态）
  python ragflow_monitor.py check ... --deep --disk-path /data

  # 持续监控，失败率超阈值告警，可配 webhook
  python ragflow_monitor.py watch ... --interval 300 --max-fail-rate 0.05 \
      --webhook https://example.com/hook

  # 机器可读
  python ragflow_monitor.py check ... --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ragflow_toolkit import RFClient  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

STATUS = {0: "未开始", 1: "解析中", 2: "已取消", 3: "已完成", 4: "失败"}
RUN_MAP = {"UNSTART": "未开始", "RUNNING": "解析中", "CANCEL": "已取消",
           "DONE": "已完成", "FAIL": "失败"}


def _status(doc: dict) -> str:
    r = doc.get("run")
    if isinstance(r, str):
        return RUN_MAP.get(r.upper(), r.upper())
    s = doc.get("status")
    if isinstance(s, int):
        return STATUS.get(s, str(s))
    return "未知"


def _all_docs(client, ds_id: str) -> list:
    docs, page = [], 1
    while True:
        r = client.get(f"/api/v1/datasets/{ds_id}/documents?page={page}&page_size=100")
        data = r.get("data", {}) or {}
        batch = data.get("docs", []) or []
        total = data.get("total", 0) or 0
        docs.extend(batch)
        if not batch or len(docs) >= total:
            break
        page += 1
    return docs


def collect(client: RFClient, deep: bool = False, disk_path: str | None = None) -> dict:
    t0 = time.time()
    api = client.health()
    latency_ms = round((time.time() - t0) * 1000, 1)

    report = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "api_ok": api.get("api", False),
        "latency_ms": latency_ms,
        "error": api.get("error"),
        "datasets": [],
        "disk": None,
        "alerts": [],
    }
    if not report["api_ok"]:
        report["alerts"].append(f"API 不通：{report['error']}")
        return report

    ds_list = client.list_datasets(page_size=100).get("data", []) or []
    for d in ds_list:
        ds_id, name = d.get("id"), d.get("name", "?")
        item = {"id": ds_id, "name": name, "total": 0, "by_status": {}}
        if deep:
            docs = _all_docs(client, ds_id)
            item["total"] = len(docs)
            for doc in docs:
                st = _status(doc)
                item["by_status"][st] = item["by_status"].get(st, 0) + 1
        else:
            r = client.get(f"/api/v1/datasets/{ds_id}/documents?page_size=100")
            data = r.get("data", {}) or {}
            item["total"] = data.get("total", 0) or 0
            for doc in data.get("docs", []) or []:
                st = _status(doc)
                item["by_status"][st] = item["by_status"].get(st, 0) + 1
        report["datasets"].append(item)

    if disk_path:
        try:
            u = shutil.disk_usage(disk_path)
            report["disk"] = {
                "path": disk_path,
                "total_gb": round(u.total / 1e9, 1),
                "used_gb": round(u.used / 1e9, 1),
                "free_gb": round(u.free / 1e9, 1),
                "used_pct": round(u.used / u.total * 100, 1),
            }
            if report["disk"]["used_pct"] > 90:
                report["alerts"].append(f"磁盘使用率 {report['disk']['used_pct']}% > 90%")
        except Exception as e:  # noqa: BLE001
            report["alerts"].append(f"磁盘读取失败：{e}")

    if latency_ms > 3000:
        report["alerts"].append(f"API 响应慢：{latency_ms}ms")
    return report


def fail_rate(item: dict) -> float:
    done = item["by_status"].get("已完成", 0)
    fail = item["by_status"].get("失败", 0)
    denom = done + fail
    return fail / denom if denom else 0.0


def render_md(report: dict) -> str:
    lines = [f"# RAGFlow 巡检报告", "", f"- 时间：{report['time']}",
             f"- API：{'✅ 正常' if report['api_ok'] else '❌ 不通'}"
             f"（{report['latency_ms']} ms）"]
    if report.get("disk"):
        d = report["disk"]
        lines.append(f"- 磁盘 `{d['path']}`：{d['used_gb']}/{d['total_gb']} GB"
                     f"（{d['used_pct']}%）")
    lines += ["", "| 知识库 | 文档数 | 已完成 | 失败 | 解析中 | 失败率 |",
              "|---|---|---|---|---|---|"]
    for it in report["datasets"]:
        bs = it["by_status"]
        lines.append(
            f"| {it['name']} | {it['total']} | {bs.get('已完成', 0)} | "
            f"{bs.get('失败', 0)} | {bs.get('解析中', 0)} | {fail_rate(it):.1%} |"
        )
    if report["alerts"]:
        lines += ["", "## ⚠️ 告警", ""] + [f"- {a}" for a in report["alerts"]]
    else:
        lines += ["", "✅ 无告警"]
    return "\n".join(lines)


def notify(webhook: str, text: str):
    try:
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(webhook, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        print(f"[!] webhook 失败：{e}")


def check_alerts(report: dict, max_fail_rate: float) -> list:
    alerts = list(report["alerts"])
    for it in report["datasets"]:
        fr = fail_rate(it)
        if fr > max_fail_rate and (it["by_status"].get("失败") or 0) > 0:
            alerts.append(f"{it['name']} 失败率 {fr:.1%} > {max_fail_rate:.0%}")
    return alerts


def main():
    p = argparse.ArgumentParser(description="RAGFlow 巡检/监控")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--base-url", default=os.environ.get("RAGFLOW_URL", "http://127.0.0.1"))
        sp.add_argument("--api-key", default=os.environ.get("RAGFLOW_API_KEY", ""))
        sp.add_argument("--deep", action="store_true")
        sp.add_argument("--disk-path", default=None)
        sp.add_argument("--max-fail-rate", type=float, default=0.05)
        sp.add_argument("--webhook", default=None)
        sp.add_argument("--json", action="store_true")

    ck = sub.add_parser("check", help="一次性巡检")
    common(ck)
    ck.set_defaults(func=lambda a: do_once(a, watch=False))

    wt = sub.add_parser("watch", help="持续监控")
    common(wt)
    wt.add_argument("--interval", type=int, default=300)
    wt.set_defaults(func=lambda a: do_once(a, watch=True))

    args = p.parse_args()
    args.func(args)


def do_once(args, watch: bool):
    client = RFClient(args.base_url, args.api_key)
    while True:
        report = collect(client, deep=args.deep, disk_path=args.disk_path)
        alerts = check_alerts(report, args.max_fail_rate)
        if args.json:
            report["alerts"] = alerts
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_md({**report, "alerts": alerts}))
        if alerts and args.webhook:
            notify(args.webhook, "RAGFlow 告警：\n" + "\n".join(alerts))
        if not watch:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
