.DEFAULT_GOAL := help
SHELL := /bin/bash

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
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

## ---------------------------------------------------------------- 环境
.PHONY: env
env: ## 从模板创建 deploy/.env（已存在则不覆盖）
	@test -f $(ENV_FILE) && echo "$(ENV_FILE) 已存在，跳过" \
	  || (cp deploy/.env.example $(ENV_FILE) && echo "已创建 $(ENV_FILE)，请填入 API 密钥")

## ---------------------------------------------------------------- 启停
.PHONY: up
up: env ## 完整启动：中间件 → 建表 → Kafka topic → 应用
	$(DC) -f $(COMPOSE_BASE) up -d
	./deploy/scripts/init-db.sh
	./deploy/scripts/init-kafka.sh
	$(DC) -f $(COMPOSE_APP) up -d --build
	@echo "等待服务启动…" && sleep 30
	./deploy/scripts/health-check.sh

.PHONY: up-base
up-base: env ## 只启动中间件
	$(DC) -f $(COMPOSE_BASE) up -d

.PHONY: up-app
up-app: env ## 只启动应用服务（需先起中间件）
	$(DC) -f $(COMPOSE_APP) up -d --build

.PHONY: up-gpu
up-gpu: env ## 启动 GPU 服务（ComfyUI）
	$(DC) -f $(COMPOSE_GPU) up -d

.PHONY: up-dev
up-dev: ## 最小开发依赖（MySQL + Redis + Milvus，约 12G 内存）
	docker compose -f $(COMPOSE_DEV) up -d

.PHONY: down
down: ## 停止全部服务（保留数据卷）
	-$(DC) -f $(COMPOSE_GPU) down
	-$(DC) -f $(COMPOSE_APP) down
	-$(DC) -f $(COMPOSE_BASE) down

.PHONY: clean
clean: ## 停止并删除数据卷（⚠️ 会清空数据库）
	-$(DC) -f $(COMPOSE_APP) down -v
	-$(DC) -f $(COMPOSE_BASE) down -v

.PHONY: logs
logs: ## 跟踪应用日志，如 make logs S=ai-rag
	$(DC) -f $(COMPOSE_APP) logs -f $(S)

## ---------------------------------------------------------------- 构建
.PHONY: build
build: build-java build-python ## 构建全部服务

.PHONY: build-java
build-java: ## 编译 Java 服务
	cd apps/java && mvn -B -DskipTests clean package

.PHONY: build-python
build-python: ## 安装 Python 公共库到当前环境
	pip install -e apps/python/ai-common

## ---------------------------------------------------------------- 校验
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

.PHONY: health
health: ## 存活检查
	./deploy/scripts/health-check.sh

.PHONY: ready
ready: ## 就绪检查（含依赖探测）
	./deploy/scripts/health-check.sh --ready

## ---------------------------------------------------------------- 模型
.PHONY: models
models: ## 下载常驻模型（约 7 GB）
	./deploy/scripts/download-models.sh

.PHONY: models-all
models-all: ## 下载全部模型（约 120 GB）
	./deploy/scripts/download-models.sh --all
