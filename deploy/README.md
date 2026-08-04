# 部署与本地运行

M0 基建已就位。本文是把整套系统跑起来的操作手册。

---

## 快速开始

```bash
# 1. 准备环境变量（填入 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY）
make env
vim deploy/.env

# 2. 一键启动全栈
make up
```

`make up` 依次完成：中间件启动 → 建表 → 创建 Kafka topic → 应用构建启动 → 健康检查。

---

## 分层启停

三个 compose 文件按依赖分层，可独立启停：

| 文件 | 内容 | 命令 |
|---|---|---|
| `docker-compose.base.yml` | MySQL / Redis / Kafka / ClickHouse / Milvus / SeaweedFS | `make up-base` |
| `docker-compose.app.yml` | 11 个应用服务 + Langfuse / Label Studio / Airflow / SRS | `make up-app` |
| `docker-compose.gpu.yml` | ComfyUI（+ M7 起的 vLLM） | `make up-gpu` |
| `docker-compose.dev.yml` | 最小开发依赖，约 12G 内存 | `make up-dev` |

**本地开发推荐 `make up-dev`**：只起 MySQL + Redis + Milvus，应用在 IDE 里直接跑
（所有服务的默认配置都指向 `localhost`，无需改配置）。

---

## 端口分配

| 端口 | 服务 | | 端口 | 服务 |
|---|---|---|---|---|
| 8080 | mall-gateway | | 9000 | ai-gateway (LiteLLM) |
| 8081 | mall-product | | 9001 | ai-rag |
| 8082 | mall-asset | | 9002 | ai-agent |
| 8083 | mall-dataplat | | 9003 | ai-media |
| 8084 | mall-kpi | | 9004 | ai-clip |
| 3306 | MySQL | | 9005 | ai-train |
| 6379 | Redis | | 3000 | Langfuse |
| 29092 | Kafka（宿主机） | | 8090 | Label Studio |
| 8123 | ClickHouse HTTP | | 8091 | Airflow |
| 9100 | ClickHouse 原生 | | 8188 | ComfyUI |
| 19530 | Milvus | | 1935 | SRS RTMP |
| 8333 | SeaweedFS S3 | | 1985 | SRS API |

ClickHouse 原生端口映射到 9100（而非默认 9000）以避开 ai-gateway；
SeaweedFS 的 volume 端口不映射到宿主机以避开 mall-gateway 的 8080。

---

## 两种健康探针

这是刻意的设计，Java 与 Python 两侧语义一致：

| 端点 | 语义 | 用途 |
|---|---|---|
| `/health` | **存活**。只回答"进程活着吗"，不探测任何下游，永远快速返回 | 容器 healthcheck。避免 MySQL 抖动导致容器被反复重启 |
| `/ready`（Python）<br>`/actuator/health`（Java） | **就绪**。探测 MySQL / Redis / Kafka / Milvus 等全部依赖 | 流量接入判断与监控告警 |

```bash
make health   # 存活检查（快）
make ready    # 就绪检查（含依赖探测）
```

`/ready` 区分 required 与可降级依赖——例如 Langfuse 挂了不该阻断客服服务，
所以它注册为 `required=False`，失败只标记 degraded 而不让整体 DOWN。

---

## 静态校验

不需要启动任何服务即可运行，适合放进 CI：

```bash
make check              # 全部
make check-compose      # 四个 compose 文件语法
make check-contracts    # Java / Python 的 Kafka Topic 契约是否一致
```

`check-contracts` 值得说明：两个语言各维护一份 topic 常量，漂移了不会编译报错，
只会在运行时把消息投递到不存在的 topic。它同时校验 **GPU 独占清单**——
`media.video.generate` 与 `train.job.request` 必须单分区，
用 Kafka 的分区语义充当分布式锁强制串行，否则并发消费必然 OOM。

---

## 模型下载

```bash
make models        # 常驻模型约 7 GB：FunASR / CosyVoice2 / bge-m3 / bge-reranker
make models-all    # 全部约 120 GB：追加 Wan2.2 / Qwen3-8B / Qwen-Image
```

下载后生成 `deploy/models.lock` 锁定版本。模型更新必须是显式操作——
上游模型更新会改变输出分布，导致评测结果不可比。

⚠️ FLUX / CatVTON / ControlNet / IP-Adapter 需手动放入 `$MODELS_DIR/comfyui/`。
FLUX.1-dev 为**非商用**许可，商用请改用 FLUX.1-schnell 或 Qwen-Image。

---

## 显存约束

单卡 24G 下，模型不能全部常驻（详见 [docs/14-infra.md](../docs/14-infra.md)）：

| 层级 | 显存 | 成员 |
|---|---|---|
| 常驻 | ~6G | FunASR + CosyVoice2 + bge-m3 + reranker |
| 独占轮转 | ~16G | ComfyUI |
| 全卡独占 | ~22G | Wan2.2 视频生成、QLoRA 训练（**须先卸载常驻模型**） |

因此：`comfyui` 与 `vllm` 不应同时启用；视频生成与训练安排在夜间窗口（02:00–06:00）。
升级到 48G 卡可解除全部约束。

---

## 常见问题

**MySQL 起来了但表没建？**
建表脚本只在数据卷首次初始化时自动执行。已有数据卷时手动跑 `./deploy/scripts/init-db.sh`，
它是幂等的（全部 `CREATE TABLE IF NOT EXISTS`），并会校验 30 张表是否齐全。

**服务起来了但 `/actuator/health` 显示 db DOWN？**
这是设计行为。Hikari 配了 `initialization-fail-timeout: -1`，MySQL 未就绪时服务照常启动
并暴露 `/health`，由深度探针报告依赖状态，而不是让容器反复重启。

**Kafka 连不上？**
容器内用 `kafka:9092`，宿主机用 `localhost:29092`——两个 listener 是分开配的。

**清空重来？**
`make clean` 会删除数据卷（⚠️ 数据库会被清空）。
