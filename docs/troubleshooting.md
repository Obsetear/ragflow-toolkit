# RAGFlow 排错手册

> 都是真实现场踩过的坑。按现象查。

## 1. 解析任务不入队 / `code:100 Can't access Redis`

**现象**：上传成功，`POST /chunks` 返回 `{"code": 100, "message": "Can't access Redis"}`，向量模型没有任何调用。

**原因**：解析任务靠 Redis 队列，Redis 不通任务直接丢。

**排查**：
```bash
# 在 RAGFlow 宿主机
docker ps | grep -i redis
docker exec -it <redis容器> redis-cli -a <密码> ping
# 再看 RAGFlow 容器能不能连上 redis
docker exec -it <ragflow容器> python -c "import redis; print(redis.Redis(host='redis',port=6379,password='xxx').ping())"
```

**常见根因**：
- Redis 端口被别的服务抢占（例如同机 Dify 的 Redis 用了 6379，RAGFlow 的变成了别的）
- 密码不一致（`.env` 里 `REDIS_PASSWORD` 与容器实际密码不同）
- 容器网络不通（host 网络 vs bridge）

## 2. v0.27.2 界面保存切片方法必报错

**现象**：知识库配置里改切片方法，点保存必报错。

**原因**：`page_size` 字段为 `null`，后端校验不通过。

**修复**：
```bash
python ragflow_toolkit.py patch --fix task_page_size --base-url ... --api-key ...
```
或直接调 API 把 `chunk_method` 里的 `page_size` 从 `null` 改成 `12`。

## 3. 有字段映射的知识库，聊天只返回一行

**现象**：知识库配了字段映射（field_map），聊天走 Text2SQL，只返回一行。

**原因**：RAGFlow 检测到 field_map 会切到 SQL 模式，不是 RAG 模式。

**解法**：如果不需要结构化查询，删掉 field_map；需要的话，聊天用「检索」而不是「SQL」。

## 4. 开了 Rerank，聊天就搜不到东西

**现象**：关掉 Rerank 正常，打开就搜不到；上游网关日志显示长输入返回 500。

**原因**：Rerank 模型服务对长输入报错，整个检索链路失败。

**排查**：
```bash
# 直接压 rerank 接口，构造一个长输入
curl -X POST <rerank_url> -H "Authorization: Bearer <key>" \
  -d '{"model":"<rerank模型>","query":"测试","documents":["长文本..."],"top_n":5}'
```
如果长输入 500、短输入 200，就是 rerank 服务本身的问题，先在 RAGFlow 里关掉 Rerank 保命。

## 5. 老浏览器打开 RAGFlow 白屏

**现象**：Chrome 老版本 / 国产浏览器内核低，RAGFlow 前端白屏，控制台报 `TransformStream is not defined` 或 `Intl.supportedValuesOf is not a function`。

**解法**：挂一个 polyfill 脚本（`TransformStream`、`Intl.supportedValuesOf`），在 index.html 里注入。

## 6. ARM64 / 麒麟离线部署

- 官方 `infiniflow/ragflow` **没有 arm64 镜像**，别硬拉。
- 用 `nightly-arm64-slim` 或自行构建。
- 麒麟 V10 SP3 的 glibc 较低，`vllm-ascend` 等需要用 Docker 跑，不能裸装。
- ES 引擎需要 `vm.max_map_count>=262144`：
  ```bash
  sudo sysctl -w vm.max_map_count=262144
  echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
  ```

## 7. 上传大文件卡死 / 服务挂起

- 升级到 **v0.27.0+**（修复了「解析大文件服务挂起」「文档上传卡死」）。
- 批量上传时 `--workers` 别开太大，4 是稳的；网络差就降到 2。

## 8. API 前缀 404

v0.27+ 前端走 `/api/v1`，不是老版本的 `/v1`。
- `GET /api/v1/datasets` ✅
- `GET /v1/datasets` ❌ 404

登录是 `POST /api/v1/auth/login`，密码需 RSA 加密（PKCS1v15），token 在**响应头 `Authorization`** 里，不在 body。
