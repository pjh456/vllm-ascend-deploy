SHELL := /bin/bash

HOST ?=
UP_TIMEOUT ?= 1800
STACK_DIR := $(CURDIR)

# 部署模式从 hosts/<host>/.env 的 STACK_MODE 读取；命令行 MODE=... 可覆盖。
#   pd     : Prefill / Decode 分离（默认）
#   single : 单实例聚合，直接暴露 OpenAI API
MODE ?= $(shell sed -n 's/^STACK_MODE=\([^ #]*\).*/\1/p' hosts/$(HOST)/.env 2>/dev/null | tail -n1)
MODE := $(if $(strip $(MODE)),$(strip $(MODE)),pd)

PROJECT := vllm-$(subst .,-,$(HOST))

COMPOSE := STACK_DIR=$(STACK_DIR) docker compose -p $(PROJECT) --profile $(MODE) \
             --env-file versions.env \
             --env-file hosts/$(HOST)/.env \
             -f compose.yaml -f hosts/$(HOST)/host.yaml

.PHONY: guard init check up down logs ps install-unit args up-service restart-service router

guard:
	@test -n "$(HOST)" || { echo "缺少 HOST，例如: make up HOST=1.2"; exit 1; }

init: guard
	@scripts/init.sh $(HOST) $(MODE)

check: guard
	@scripts/check.py $(HOST)
	@$(COMPOSE) config -q

up: check
	@$(COMPOSE) up -d --wait --wait-timeout $(UP_TIMEOUT)

down: guard
	@$(COMPOSE) down

logs: guard
	@$(COMPOSE) logs -f --tail=100

ps: guard
	@$(COMPOSE) ps

SERVICE ?= $(if $(filter single,$(MODE)),single,prefill)
args: guard
	@$(COMPOSE) run --rm --no-deps -e DRY_RUN=1 $(SERVICE)

# 只重建单个服务，不碰 depends_on 里的其他服务
up-service: check
	@test -n "$(SERVICE)" || { echo "缺少 SERVICE，例如: make up-service HOST=1.8 SERVICE=router"; exit 1; }
	@$(COMPOSE) up -d --no-deps --force-recreate $(SERVICE)

# 只重启单个服务（不重新读取配置）
restart-service: guard
	@test -n "$(SERVICE)" || { echo "缺少 SERVICE"; exit 1; }
	@$(COMPOSE) restart $(SERVICE)

# router 常用快捷方式
router: guard
	@$(COMPOSE) up -d --no-deps --force-recreate router

install-unit:
	sed "s#__STACK_DIR__#$(CURDIR)#g" systemd/vllm-stack@.service \
	  | sudo tee /etc/systemd/system/vllm-stack@.service >/dev/null
	sudo systemctl daemon-reload
	@echo "启用: sudo systemctl enable --now vllm-stack@<host>"
