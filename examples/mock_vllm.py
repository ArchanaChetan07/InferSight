"""A mock vLLM server for demos and integration tests — no GPU required.

Implements just enough of the OpenAI-compatible surface:
  * GET  /v1/models
  * POST /v1/completions and /v1/chat/completions (streaming + non-streaming)
  * GET  /metrics with vllm:-style gauges (KV cache, queue depth)

Latency is simulated with a configurable TTFT and per-token delay so the
sidecar has something realistic to measure.

Run:  python examples/mock_vllm.py --port 8000 --ttft 0.12 --tbt 0.02
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

app = FastAPI(title="mock-vllm")
STATE = {"ttft": 0.08, "tbt": 0.015, "model": "meta-llama/Llama-3.1-8B-Instruct", "kv": 0.35}


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": STATE["model"], "object": "model"}]})


@app.get("/metrics")
async def metrics():
    kv = min(0.99, max(0.05, STATE["kv"] + random.uniform(-0.05, 0.07)))
    STATE["kv"] = kv
    body = (
        f'vllm:gpu_cache_usage_perc{{model_name="{STATE["model"]}"}} {kv:.4f}\n'
        f'vllm:num_requests_running{{model_name="{STATE["model"]}"}} {random.randint(1, 8)}\n'
        f'vllm:num_requests_waiting{{model_name="{STATE["model"]}"}} {random.randint(0, 4)}\n'
        f'vllm:num_requests_swapped{{model_name="{STATE["model"]}"}} 0\n'
    )
    return PlainTextResponse(body)


@app.post("/v1/completions")
@app.post("/v1/chat/completions")
async def completions(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
    n_tokens = int(payload.get("max_tokens") or random.randint(20, 60))
    model = payload.get("model", STATE["model"])
    chat = request.url.path.endswith("chat/completions")
    rid = ("chatcmpl-" if chat else "cmpl-") + uuid.uuid4().hex[:12]
    prompt_tokens = random.randint(50, 400)

    if payload.get("stream"):
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))

        async def gen():
            await asyncio.sleep(STATE["ttft"] * random.uniform(0.8, 1.3))
            for i in range(n_tokens):
                delta = {"content": f"tok{i} "} if chat else None
                chunk = {
                    "id": rid, "object": "chat.completion.chunk" if chat else "text_completion",
                    "created": int(time.time()), "model": model,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None} if chat
                        else {"index": 0, "text": f"tok{i} ", "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                await asyncio.sleep(STATE["tbt"] * random.uniform(0.6, 1.6))
            if include_usage:
                usage_chunk = {
                    "id": rid, "object": "chat.completion.chunk" if chat else "text_completion",
                    "created": int(time.time()), "model": model, "choices": [],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": n_tokens,
                              "total_tokens": prompt_tokens + n_tokens},
                }
                yield f"data: {json.dumps(usage_chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    await asyncio.sleep(STATE["ttft"] + STATE["tbt"] * n_tokens)
    text = " ".join(f"tok{i}" for i in range(n_tokens))
    return JSONResponse({
        "id": rid, "object": "chat.completion" if chat else "text_completion",
        "created": int(time.time()), "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            if chat else
            {"index": 0, "text": text, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": n_tokens,
                  "total_tokens": prompt_tokens + n_tokens},
    })


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ttft", type=float, default=0.08)
    parser.add_argument("--tbt", type=float, default=0.015)
    args = parser.parse_args()
    STATE["ttft"], STATE["tbt"] = args.ttft, args.tbt
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
