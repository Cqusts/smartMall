.DEFAULT_GOAL := help
SHELL := /bin/bash

PY := python3
RUN_JAVA := $(PY) deploy/scripts/run-java.py

COMPOSE_BASE := deploy/docker-compose.base.yml
COMPOSE_APP  := deploy/docker-compose.app.yml
COMPOSE_GPU  := deploy/docker-compose.gpu.yml
COMPOSE_DEV  := deploy/docker-compose.dev.yml
ENV_FILE     := deploy/.env

DC := docker compose --env-file $(ENV_FILE)

## ---------------------------------------------------------------- 帮助
.PHONY: help
help:
	@echo "smartMall —— 多模态电商 AI 体系"
	@echo
	@echo "本地开发（只要 JDK 21 + 本机 MySQL，不用 Docker）："
	@echo "    make db-init && make build && make up && make serve"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

## ---------------------------------------------------------------- 数据库（本机 MySQL）
.PHONY: db-init
db-init: ## 建库 + 建应用账号 + 建表 + 跑迁移（可反复执行）
	@# 管理员密码走 MYSQL_ADMIN_PASSWORD，**不要**用 MYSQL_PASSWORD ——
	@# 后者是应用账号的密码，混用会拼出 smartmall/<root密码> 这种不存在的组合。
	$(PY) deploy/scripts/migrate.py

.PHONY: db-status
db-status: ## 看哪些迁移还没应用
	$(PY) deploy/scripts/migrate.py --status

## ---------------------------------------------------------------- Java 服务（本地）
.PHONY: build
build: ## 构建 5 个服务的 jar
	$(RUN_JAVA) build

.PHONY: up
up: ## 后台起全部 Java 服务并等就绪（make up S=mall-product 只起一个）
	$(RUN_JAVA) up $(S)

.PHONY: down
down: ## 停掉 Java 服务
	$(RUN_JAVA) down $(S)

.PHONY: restart
restart: ## 重起
	$(RUN_JAVA) restart $(S)

.PHONY: status
status: ## 看服务状态（含与 MySQL 的连通性）
	$(RUN_JAVA) status

.PHONY: run
run: ## 前台起一个服务，如 make run S=mall-product
	$(RUN_JAVA) run $(S)

.PHONY: logs
logs: ## 看日志尾部，如 make logs S=mall-product
	$(RUN_JAVA) logs $(S)

## ---------------------------------------------------------------- 店铺页 / Python
.PHONY: serve
serve: ## 起店铺页 :9002（前台）
	MYSQL_HOST=$${MYSQL_HOST:-127.0.0.1} \
	MYSQL_USER=$${MYSQL_USER:-smartmall} \
	MYSQL_PASSWORD=$${MYSQL_PASSWORD:-smartmall} \
	MYSQL_DATABASE=$${MYSQL_DATABASE:-smartmall} \
	smartmall-agent serve

.PHONY: build-python
build-python: ## 安装 Python 包到当前环境（顺序不能变，本地包先）
	pip install -e pipelines -e apps/python/ai-common -e "apps/python/ai-agent[server]"

## ---------------------------------------------------------------- 校验
.PHONY: doctor
doctor: ## 环境自检：JDK、数据库、账号、迁移、端口
	$(PY) deploy/scripts/doctor.py

.PHONY: verify
verify: ## 对真库复核订单链路（状态机 + 防超卖），需先 make up
	$(PY) deploy/scripts/verify-orders.py

.PHONY: test
test: ## 跑 Java 与 ai-agent 测试
	cd apps/java && ./mvnw -B test
	$(PY) -m pytest -q apps/python/ai-agent/tests

.PHONY: check
check: check-compose check-contracts ## 全部静态校验

.PHONY: check-compose
check-compose: ## 校验 compose 文件语法
	@for f in $(COMPOSE_BASE) $(COMPOSE_APP) $(COMPOSE_GPU) $(COMPOSE_DEV); do \
	  docker compose -f $$f --env-file deploy/.env.example config -q \
	    && echo "  ✓ $$f" || exit 1; \
	done

.PHONY: check-contracts
check-contracts: ## 校验 Java / Python 的 Kafka Topic 契约是否一致
	./deploy/scripts/check-contracts.sh

## ---------------------------------------------------------------- 模型
.PHONY: models
models: ## 下载常驻模型（约 7 GB）
	./deploy/scripts/download-models.sh

.PHONY: models-all
models-all: ## 下载全部模型（约 120 GB）
	./deploy/scripts/download-models.sh --all

## ---------------------------------------------------------------- 整套部署（Docker Compose）
##
## 本地开发用不到这一节。这里是把全套中间件（Kafka / Redis / Milvus / MinIO /
## ClickHouse…）与全部服务一起拉起来的部署方式，机器要求高得多。
##
## 目标名都带 docker- 前缀，是为了不和上面的本地目标撞名 —— 之前 `make up`
## 指的是「起 Docker 全家桶」，改成本地之后如果不改名，同一条命令在两个人
## 手里做的是完全不同的事。
.PHONY: docker-env
docker-env: ## 从模板创建 deploy/.env（已存在则不覆盖）
	@test -f $(ENV_FILE) && echo "$(ENV_FILE) 已存在，跳过" \
	  || (cp deploy/.env.example $(ENV_FILE) && echo "已创建 $(ENV_FILE)，请填入 API 密钥")

.PHONY: docker-up
docker-up: docker-env ## 完整启动：中间件 → 建表 → Kafka topic → 应用
	$(DC) -f $(COMPOSE_BASE) up -d
	./deploy/scripts/init-db.sh
	./deploy/scripts/init-kafka.sh
	$(DC) -f $(COMPOSE_APP) up -d --build
	@echo "等待服务启动…" && sleep 30
	./deploy/scripts/health-check.sh

.PHONY: docker-up-base
docker-up-base: docker-env ## 只启动中间件
	$(DC) -f $(COMPOSE_BASE) up -d

.PHONY: docker-up-gpu
docker-up-gpu: docker-env ## 启动 GPU 服务（ComfyUI）
	$(DC) -f $(COMPOSE_GPU) up -d

.PHONY: docker-down
docker-down: ## 停止全部容器（保留数据卷）
	-$(DC) -f $(COMPOSE_GPU) down
	-$(DC) -f $(COMPOSE_APP) down
	-$(DC) -f $(COMPOSE_BASE) down

.PHONY: docker-clean
docker-clean: ## 停止并删除数据卷（⚠️ 会清空数据库）
	-$(DC) -f $(COMPOSE_APP) down -v
	-$(DC) -f $(COMPOSE_BASE) down -v

.PHONY: docker-logs
docker-logs: ## 跟踪容器日志，如 make docker-logs S=ai-rag
	$(DC) -f $(COMPOSE_APP) logs -f $(S)

.PHONY: docker-health
docker-health: ## 容器版存活检查
	./deploy/scripts/health-check.sh
