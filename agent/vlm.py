"""OpenAI-compatible vision-language model client (default: the local vLLM on the GB10)."""
import base64
import io
import json
import os
import subprocess
import time
import urllib.request


def load_key(key_file):
    """API key from $AIKVM_LLM_KEY, else the key file (directly, or via passwordless sudo). Never cached on disk."""
    if os.environ.get("AIKVM_LLM_KEY"):
        return os.environ["AIKVM_LLM_KEY"]
    if not key_file:
        return None
    try:
        with open(key_file) as f:
            return f.read().strip()
    except PermissionError:
        out = subprocess.run(["sudo", "-n", "cat", key_file], capture_output=True, text=True)
        return out.stdout.strip() or None
    except FileNotFoundError:
        return None


class VLM:
    def __init__(self, base, model, key=None, max_side=1280, quality=80):
        self.url = base.rstrip("/") + "/chat/completions"
        self.model = model
        self.key = key
        self.max_side = max_side
        self.quality = quality

    def sent_size(self, img):
        """Size of the picture the model receives (image_part downscales to max_side)."""
        if max(img.size) <= self.max_side:
            return img.size
        s = self.max_side / max(img.size)
        return round(img.width * s), round(img.height * s)

    def image_part(self, img):
        if max(img.size) > self.max_side:
            img = img.resize(self.sent_size(img))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality)
        data = base64.b64encode(buf.getvalue()).decode()
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}}

    def chat(self, messages, max_tokens=200, schema=None):
        """(reply text, usage, seconds). schema: JSON schema the reply is constrained to (vLLM structured outputs)."""
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False}}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.load(r)
        return out["choices"][0]["message"]["content"], out.get("usage", {}), time.monotonic() - t0
