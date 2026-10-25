#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地图像生成代理（OpenAI 兼容），把智谱 CogView 的「AI生成」角标在返回前抹掉。

为什么要这一层：
    dsh-draw 插件只会把上游原样返回的字节存成附件，自己没有后处理；
    而角标是智谱在服务端烧进像素的。所以在我们和智谱之间放一个 127.0.0.1 的小代理，
    它去取图 → 去角标 → 转成无损 PNG → 以 b64_json 回给插件。
    插件的输出从此天生干净，GUI 里点图、我在会话里调 image_generate 都走同一条链。

端点：
    GET  /health                    → {"ok":true,...}
    GET  /v1/models                 → 兼容探测
    POST /v1/images/generations     → {"data":[{"b64_json":"..."}]}

启动：
    python image-shim.py            # 默认 127.0.0.1:3089
    SHIM_PORT=3090 python image-shim.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dewatermark import clean_image  # noqa: E402

from PIL import Image  # noqa: E402

PORT = int(os.environ.get("SHIM_PORT", "3089"))
UPSTREAM = "https://open.bigmodel.cn/api/paas/v4/images/generations"
CRED = os.path.expanduser(r"~\.dsh\.credentials.yaml")
LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "draws")
ALLOWED = {"1024x1024", "768x1344", "1344x768", "864x1152", "1152x864", "1440x960", "960x1440"}

STARTED = time.time()
STATE: dict = {"requests": 0, "last": None}


def log(msg: str) -> None:
    os.makedirs(LOGDIR, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOGDIR, "shim.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def glm_key() -> str | None:
    env = os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPU_API_KEY")
    if env:
        return env.strip()
    if not os.path.exists(CRED):
        return None
    txt = open(CRED, "r", encoding="utf-8", errors="replace").read()
    for name in ("GLM_API_KEY", "ZHIPU_API_KEY"):
        m = re.search(rf"^\s*{name}\s*:\s*(\S+)\s*$", txt, re.M)
        if m:
            return m.group(1).strip().strip('"\'')
    return None


def upstream_one(key: str, prompt: str, size: str, model: str) -> bytes:
    body = json.dumps({"model": model, "prompt": prompt, "size": size}).encode()
    req = urllib.request.Request(
        UPSTREAM, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    url = (payload.get("data") or [{}])[0].get("url")
    if not url:
        raise RuntimeError("upstream returned no url: " + json.dumps(payload)[:200])
    with urllib.request.urlopen(url, timeout=120) as img:
        return img.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *a) -> None:  # 静音默认 access log
        pass

    def _status(self) -> dict:
        return {
            "ok": True,
            "shim": {
                "port": PORT,
                "pid": os.getpid(),
                "uptimeSec": int(time.time() - STARTED),
                "engine": "zhipu cogview-3-flash",
                "watermark": "strip",
                "upstream": "open.bigmodel.cn",
            },
            "requests": STATE["requests"],
            "last": STATE["last"],
            "credential": {"present": bool(glm_key()), "ref": "GLM_API_KEY"},
            "sizes": sorted(ALLOWED),
        }

    def do_OPTIONS(self) -> None:  # 只读面板跨域预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", "/v1/health"):
            self._send(200, {"ok": True, "engine": "zhipu-cogview-clean", "watermark": "removed", "port": PORT})
        elif path == "/draw/status":
            self._send(200, self._status())
        elif path.endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "cogview-3-flash-clean", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found", "path": self.path}})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/images/generations"):
            self._send(404, {"error": {"message": "not found", "path": self.path}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send(400, {"error": {"message": "bad json body"}})
            return

        key = glm_key()
        if not key:
            self._send(500, {"error": {"message": "no GLM_API_KEY in env or ~/.dsh/.credentials.yaml"}})
            return

        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            self._send(400, {"error": {"message": "prompt is required"}})
            return
        n = max(1, min(4, int(body.get("n") or 1)))
        size = str(body.get("size") or "1024x1024").lower()
        if size not in ALLOWED:
            size = "1024x1024"
        model = str(body.get("model") or "cogview-3-flash")
        upstream_model = "cogview-3-flash" if "clean" in model or not model else model
        if upstream_model not in ("cogview-3-flash", "cogview-3-plus", "cogview-4-250304"):
            upstream_model = "cogview-3-flash"

        STATE["requests"] += 1
        started = time.time()
        out: list[dict] = []
        try:
            for _ in range(n):
                raw = upstream_one(key, prompt, size, upstream_model)
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                clean, box, action = clean_image(im)
                buf = io.BytesIO()
                clean.save(buf, format="PNG")
                out.append({"b64_json": base64.b64encode(buf.getvalue()).decode()})
                log(f"gen ok size={size} n={len(out)} action={action} box={box} bytes={len(raw)}->{buf.tell()}")
        except Exception as exc:  # noqa: BLE001
            log(f"gen FAIL {exc!r}")
            self._send(502, {"error": {"message": f"upstream failed: {exc}"}})
            return

        STATE["last"] = {
            "ts": int(time.time()),
            "size": size,
            "n": n,
            "ms": int((time.time() - started) * 1000),
            "prompt": prompt[:60],
        }
        log(f"request done in {int((time.time() - started) * 1000)}ms n={n}")
        self._send(200, {"created": int(time.time()), "data": out})


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"image-shim listening on http://127.0.0.1:{PORT}  (upstream=zhipu cogview-3-flash, watermark=strip)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
