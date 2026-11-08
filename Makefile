SHELL := /bin/bash

HOST ?=
STACK_DIR := $(CURDIR)

COMPOSE := STACK_DIR=$(STACK_DIR) docker compose -p vllm-$(HOST) \
             --env-file versions.env \
             --env-file hosts/$(HOST)/.env \
             -f compose.yaml -f hosts/$(HOST)/host.yaml

.PHONY: guard init check up down logs ps install-unit

guard:
	@test -n "$(HOST)" || { echo "缺少 HOST，例如: make up HOST=1.2"; exit 1; }

init: guard
	@scripts/init.sh $(HOST)

check: guard
	@scripts/check.py $(HOST)
	@$(COMPOSE) config -q

up: check
	@$(COMPOSE) up -d --wait --wait-timeout 1800

down: guard
	@$(COMPOSE) down

logs: guard
	@$(COMPOSE) logs -f --tail=100

ps: guard
	@$(COMPOSE) ps

install-unit:
	sed "s#__STACK_DIR__#$(CURDIR)#g" systemd/vllm-stack@.service \
	  | sudo tee /etc/systemd/system/vllm-stack@.service >/dev/null
	sudo systemctl daemon-reload
	@echo "启用: sudo systemctl enable --now vllm-stack@<host>"
