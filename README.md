# ragflow-toolkit

> 零依赖 RAGFlow 运维工具箱：百万级批量导入、断点续传、连通性体检、已知 bug 修补。

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

RAGFlow 很好用，但在**真实的内网环境**里批量灌数据时会遇到一堆坑：几十万文件传到一半断了、Windows 长路径报错、解析任务不入队、v0.27.2 保存切片方法必报错……这个工具箱把我们踩过的坑一次性解决。

## 解决什么问题

| 痛点 | 解法 |
|---|---|
| 61 万文件传到一半断网，重跑全部重传 | SQLite 断点续传，重跑只补未完成的 |
| Windows 路径超 260 字符报错 | 自动加 `\\?\` 长路径前缀 |
| 多个顶层目录要进不同知识库 | `--map` 文件按路径前缀映射 |
| 上传成功但解析没触发 | 自动增量触发解析，补解析 `parsed=0` |
| 现场机装不了 pip 包 | 纯标准库，拷过去就能跑 |
| v0.27.2 保存切片方法报错 | `patch --fix task_page_size` 一键修 |
| 解析卡住不知道查哪 | `health` 一键体检 Redis/ES/API |

## 快速开始

```bash
# 1. 体检：确认 API / Redis / ES 都通
python ragflow_toolkit.py health \
  --base-url http://10.0.0.10:9380 \
  --api-key ragflow-xxxxxxxx

# 2. 批量导入（断点续传，重跑自动跳过已传）
python ragflow_toolkit.py upload \
  --base-url http://10.0.0.10:9380 \
  --api-key ragflow-xxxxxxxx \
  --source D:\data\裁判文书 \
  --dataset 裁判文书 \
  --workers 4

# 3. 看进度
python ragflow_toolkit.py status

# 4. 多个目录进不同库
# map.txt:
#   学习培训|学习库
#   项目资料|项目库
python ragflow_toolkit.py upload ... --map map.txt --dataset 默认库
```

> 也支持环境变量：`RAGFLOW_URL`、`RAGFLOW_API_KEY`。

## 设计取舍

- **纯标准库**：现场机（Win7 / 麒麟 / 无外网）往往装不了包。只用 `urllib`、`sqlite3`、`concurrent.futures`。
- **SQLite 而不是 JSON**：60 万级状态用 JSON 会吃掉几个 G 内存，SQLite 稳定在几十 M。
- **源文件只读**：永不改动源目录。
- **增量解析**：每 `PARSE_EVERY` 个触发一次，结束补尾巴，避免重复解析。

## 已知 bug 修补

### v0.27.2 保存切片方法必报错

界面里改切片方法会报错，根因是 `page_size` 为 `null`。修复：

```bash
python ragflow_toolkit.py patch --fix task_page_size --base-url ... --api-key ...
```

### 解析返回 `code:100` + `Can't access Redis`

解析任务没入队，向量模型根本不会被调用。先查：

```bash
python ragflow_toolkit.py health ...
```

更多排错见 [docs/troubleshooting.md](docs/troubleshooting.md)。

## 适用版本

- RAGFlow **v0.27.x**（API 前缀 `/api/v1`）
- 更老版本（`/v1`）需要改 `RFClient` 里的路径前缀

## License

MIT

---

*If you find this useful, a ⭐ helps others find it.*
