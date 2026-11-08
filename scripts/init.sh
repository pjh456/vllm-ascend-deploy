#!/usr/bin/env bash
# 为指定主机生成 hosts/<host>/ 下的真实配置（全部被 .gitignore 忽略）
set -euo pipefail

HOST="${1:?用法: init.sh <host>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f versions.env ]]; then
  cp versions.env.example versions.env
  echo "created versions.env（建议改为 digest 固定镜像）"
fi

DST="hosts/${HOST}"
if [[ -e "$DST" ]]; then
  echo "${DST} 已存在，拒绝覆盖" >&2
  exit 1
fi

mkdir -p "$DST"
cp hosts/.env.example "$DST/.env"
cp hosts/host.yaml.example "$DST/host.yaml"

for f in kv-p.json kv-d.json p-additional.json d-additional.json \
         mmc-local.conf mmc-meta.conf mooncake.json runtime.env; do
  cp "hosts/${f}.example" "$DST/${f}"
done

sed -i "s/__HOST__/${HOST}/g" "$DST/.env"

echo "created ${DST}"
echo "替换 ${DST}/.env 与同目录文件里的 __占位符__，然后执行: make check HOST=${HOST}"
