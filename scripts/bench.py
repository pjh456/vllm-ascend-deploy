#!/usr/bin/env python3
"""A tiny streaming load generator for the deployed vLLM endpoint.

Reports the numbers that matter for decode speed: TTFT, TPOT/ITL and
throughput, so different stack configurations can be compared without
guessing.  Only needs `requests` (already present on the deploy host); it
does not import vLLM, so it runs on the host next to the container.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import statistics
import time

import requests

SAMPLE_TEXT = (
    "The quick brown fox jumps over the lazy dog while the deployment keeps "
    "streaming tokens at a steady rate. "
)


def build_prompt(base_url: str, model: str, target_tokens: int, timeout: float) -> str:
    """Grow a synthetic prompt until the server tokenizer reports the target size."""
    text = SAMPLE_TEXT
    for _ in range(64):
        n = count_tokens(base_url, model, text, timeout)
        if n >= target_tokens:
            return fit_prompt(base_url, model, text, target_tokens, timeout)
        text = text + text
    return text


def fit_prompt(base_url: str, model: str, text: str, target_tokens: int, timeout: float) -> str:
    tokens = count_tokens(base_url, model, text, timeout)
    if tokens <= 0:
        return text
    chars_per_token = len(text) / tokens
    approx = int(target_tokens * chars_per_token)
    return text[: max(approx, 1)]


def count_tokens(base_url: str, model: str, text: str, timeout: float) -> int:
    try:
        resp = requests.post(
            f"{base_url}/tokenize", json={"model": model, "prompt": text}, timeout=timeout
        )
        resp.raise_for_status()
        return int(resp.json()["count"])
    except Exception:
        # Rough fallback: 4 characters per token.
        return max(len(text) // 4, 1)


def one_request(base_url: str, model: str, prompt: str, output_len: int, timeout: float) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": output_len,
        "ignore_eos": True,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    start = time.perf_counter()
    ttft = None
    stamps: list[float] = []
    prompt_tokens = 0
    with requests.post(
        f"{base_url}/v1/completions", json=payload, stream=True, timeout=timeout
    ) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore")
            if not line.startswith("data: "):
                continue
            body = line[len("data: ") :]
            if body.strip() == "[DONE]":
                break
            chunk = json.loads(body)
            usage = chunk.get("usage") or {}
            if usage.get("prompt_tokens"):
                prompt_tokens = int(usage["prompt_tokens"])
            choices = chunk.get("choices") or []
            if not choices:
                continue
            text = choices[0].get("text") or ""
            if not text:
                continue
            now = time.perf_counter()
            if ttft is None:
                ttft = now - start
            stamps.append(now)
    end = time.perf_counter()
    if ttft is None:
        raise RuntimeError("no tokens streamed")
    itls = [b - a for a, b in zip(stamps, stamps[1:])]
    return {
        "prompt_tokens": prompt_tokens,
        "output_tokens": len(stamps),
        "ttft_ms": ttft * 1e3,
        "tpot_ms": statistics.fmean(itls) * 1e3 if itls else float("nan"),
        "itls_ms": [x * 1e3 for x in itls],
        "elapsed_s": end - start,
    }


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(int(q * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--num-prompts", type=int, default=8)
    parser.add_argument("--input-len", type=int, default=1024)
    parser.add_argument("--output-len", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    random.seed(args.seed)
    base_url = args.url.rstrip("/")
    prompt = build_prompt(base_url, args.model, args.input_len, args.timeout)

    results, errors = [], []
    wall = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(
                one_request, base_url, args.model, prompt, args.output_len, args.timeout
            )
            for _ in range(args.num_prompts)
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report and keep the rest
                errors.append(repr(exc))
    wall = time.perf_counter() - wall

    if not results:
        print(json.dumps({"errors": errors}, indent=2))
        return 1

    ttfts = [r["ttft_ms"] for r in results]
    tpots = [r["tpot_ms"] for r in results if r["tpot_ms"] == r["tpot_ms"]]
    itls = [x for r in results for x in r["itls_ms"]]
    out_tokens = sum(r["output_tokens"] for r in results)
    prompt_tokens = max(r["prompt_tokens"] for r in results)
    summary = {
        "requests": len(results),
        "errors": errors,
        "concurrency": args.concurrency,
        "prompt_tokens": prompt_tokens,
        "output_tokens_per_request": args.output_len,
        "wall_s": round(wall, 3),
        "output_tokens_per_s": round(out_tokens / wall, 2),
        "total_tokens_per_s": round((out_tokens + prompt_tokens * len(results)) / wall, 2),
        "ttft_ms_p50": round(pct(ttfts, 0.5), 2),
        "ttft_ms_p90": round(pct(ttfts, 0.9), 2),
        "tpot_ms_p50": round(pct(tpots, 0.5), 2),
        "tpot_ms_p90": round(pct(tpots, 0.9), 2),
        "itl_ms_p50": round(pct(itls, 0.5), 2),
        "itl_ms_p90": round(pct(itls, 0.9), 2),
        "decode_tokens_per_s_per_seq": round(1e3 / pct(tpots, 0.5), 2) if tpots else None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
