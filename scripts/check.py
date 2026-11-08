#!/usr/bin/env python3
"""主机配置自检：在起容器之前把能查的错全部查掉。"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")
JSON_FILES = ("kv-p.json", "kv-d.json", "p-additional.json", "d-additional.json", "mooncake.json")
PORT_KEYS = ("P_PORT", "D_PORT", "ROUTER_PORT", "METRICS_PORT")


def parse_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            out[key.strip()] = value.strip().strip("'").strip('"')
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: check.py <host>", file=sys.stderr)
        return 2
    host = sys.argv[1]
    conf = os.path.join(ROOT, "hosts", host)
    env_file = os.path.join(conf, ".env")
    errors: list[str] = []

    if not os.path.isfile(env_file):
        print(f"缺少 {env_file}，先执行 make init HOST={host}", file=sys.stderr)
        return 2

    tmpl = parse_env(os.path.join(ROOT, "hosts", ".env.example"))
    real = parse_env(env_file)

    missing = sorted(set(tmpl) - set(real))
    if missing:
        errors.append(f".env 缺少键: {', '.join(missing)}")

    unset = sorted(k for k, v in real.items() if "__" in v)
    if unset:
        errors.append(f".env 仍有占位符: {', '.join(unset)}")

    if not os.path.isfile(os.path.join(ROOT, "env", "common.env")):
        errors.append("缺少 env/common.env")

    rt_tmpl_path = os.path.join(ROOT, "hosts", "runtime.env.example")
    rt_path = os.path.join(conf, "runtime.env")
    if os.path.isfile(rt_tmpl_path):
        rt_tmpl = parse_env(rt_tmpl_path)
        if not os.path.isfile(rt_path):
            errors.append("缺少 runtime.env")
        else:
            rt_real = parse_env(rt_path)
            missing_rt = sorted(set(rt_tmpl) - set(rt_real))
            if missing_rt:
                errors.append(f"runtime.env 缺少键: {', '.join(missing_rt)}")

    ports = {k: real.get(k, "") for k in PORT_KEYS}
    if len(set(ports.values())) != len(ports):
        errors.append(f"端口重复: {ports}")

    model_root = real.get("MODEL_ROOT", "")
    for key in ("MODEL", "DRAFT"):
        path = real.get(key, "")
        if not path:
            continue
        if not os.path.exists(path):
            errors.append(f"{key} 路径不存在: {path}")
        if model_root and not path.startswith(model_root.rstrip(os.sep) + os.sep):
            errors.append(f"{key} 不在 MODEL_ROOT={model_root} 之下，容器内不可见")

    # @file 形式的参数值：容器内路径 /opt/vllm/... 映射回仓库根目录
    for key, value in real.items():
        if not value.startswith("@"):
            continue
        path = value[1:]
        if path.startswith("/opt/vllm/"):
            path = os.path.join(ROOT, path[len("/opt/vllm/"):])
        if not os.path.isfile(path):
            errors.append(f"{key} 指向的文件不存在: {value}")

    for key in ("P_CAPTURE_MODE", "D_CAPTURE_MODE"):
        if not real.get(key):
            errors.append(f"{key} 未设置")

    for name in JSON_FILES:
        path = os.path.join(conf, name)
        if not os.path.isfile(path):
            errors.append(f"缺少 {name}")
            continue
        text = open(path, encoding="utf-8").read()
        hit = PLACEHOLDER.findall(text)
        if hit:
            errors.append(f"{name} 仍有占位符: {', '.join(sorted(set(hit)))}")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"{name} 不是合法 JSON: {exc}")

    kvp_path = os.path.join(conf, "kv-p.json")
    if os.path.isfile(kvp_path):
        try:
            kvp = json.load(open(kvp_path, encoding="utf-8"))
            mc = kvp["kv_connector_extra_config"]["connectors"][0]
            extra = mc["kv_connector_extra_config"]
            tp = int(real.get("COMMON_TP") or 0)
            for role in ("prefill", "decode"):
                if int(extra[role]["tp_size"]) != tp:
                    errors.append(f"kv-p.json {role}.tp_size 与 TP={tp} 不一致")
            bootstrap = str(real.get("KV_BOOTSTRAP_PORT", "")).strip()
            if bootstrap.lower() not in ("", "none") and str(mc["kv_port"]) != bootstrap:
                errors.append(
                    f"kv-p.json kv_port={mc['kv_port']} 与 KV_BOOTSTRAP_PORT={bootstrap} 不一致"
                )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"kv-p.json 结构异常: {exc}")

    mmc_path = os.path.join(conf, "mmc-local.conf")
    if os.path.isfile(mmc_path):
        match = re.search(r"world_size\s*=\s*(\d+)", open(mmc_path, encoding="utf-8").read())
        if not match:
            errors.append("mmc-local.conf 缺少 world_size")
        elif int(match.group(1)) < tp * 2:
            errors.append(f"mmc-local.conf world_size 小于 TP*2={tp * 2}")

    if tp <= 0:
        errors.append("COMMON_TP 未设置")

    step = int(real.get("SPEC_TOKENS") or 0) + 1
    seqs = int(real.get("COMMON_MAX_NUM_SEQS") or 0)
    print(f"capture sizes: {step}..{step * seqs}，共 {seqs} 个（步长 {step}）")

    if errors:
        for item in errors:
            print(f"ERROR: {item}", file=sys.stderr)
        return 1
    print(f"check ok: hosts/{host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
