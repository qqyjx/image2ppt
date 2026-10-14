"""Model backends behind one small interface.

A backend receives a prompt, a JSON schema and a list of image files, and
returns a dict that follows the schema. The pipeline never imports anything
else from this module, so a different model can be plugged in by implementing
`LLMClient.complete_json`.

Backends:
  ClaudeCLIClient    local `claude` CLI in non-interactive mode (default)
  AnthropicAPIClient Anthropic Messages API, key read from ANTHROPIC_API_KEY
  ReplayClient       returns recorded outputs; used by tests and the demo
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, Sequence


class LLMClient(Protocol):
    """Structured vision call: prompt + images in, JSON object out."""

    def complete_json(self, prompt: str, schema: dict, images: Sequence[str | Path] = ()) -> dict:
        ...


class ClaudeCLIClient:
    """Runs `claude -p` and reads the images through the CLI's Read tool.

    The CLI uses the account it is already logged in with; no API key is read
    or passed by this class.
    """

    def __init__(self, model: str | None = "sonnet", max_turns: int = 8, timeout: int = 900):
        self.model, self.max_turns, self.timeout = model, max_turns, timeout

    def complete_json(self, prompt: str, schema: dict, images: Sequence[str | Path] = ()) -> dict:
        if images:
            listing = "\n".join(f"- {Path(p).resolve()}" for p in images)
            prompt += "\n\nAttached image files, in order (open each with the Read tool):\n" + listing
        cmd = [
            "claude", "-p", prompt,
            "--tools", "Read",
            "--max-turns", str(self.max_turns),
            "--output-format", "json",
            # Load project settings only, so user-level hooks do not run during this call.
            "--setting-sources", "project",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--json-schema", json.dumps(schema),
        ]
        if self.model:
            cmd += ["--model", self.model]
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow starting the CLI from inside another CLI session
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout,
                             env=env, stdin=subprocess.DEVNULL)
        if res.returncode != 0:
            raise RuntimeError(f"claude -p failed rc={res.returncode}: {res.stderr[-800:]}")
        data = json.loads(res.stdout)
        if isinstance(data, list):
            data = [x for x in data if x.get("type") == "result"][-1]
        if data.get("is_error") or "structured_output" not in data:
            raise RuntimeError(f"no structured output: {str(data.get('result'))[:800]}")
        sys.stderr.write(f"[llm] turns={data.get('num_turns')} {data.get('duration_ms', 0) / 1000:.0f}s\n")
        return data["structured_output"]


class AnthropicAPIClient:
    """Anthropic Messages API over HTTPS (standard library only).

    The schema is passed as the input schema of a single tool that the model
    is forced to call; the tool arguments are the structured output. The key
    comes from the ANTHROPIC_API_KEY environment variable and is never stored,
    printed or put in an exception message.
    """

    URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-sonnet-4-5"

    def __init__(self, model: str | None = None, max_tokens: int = 16000, timeout: int = 600,
                 url: str | None = None):
        self.model = model or os.environ.get("IMAGE2PPT_MODEL") or self.DEFAULT_MODEL
        self.max_tokens, self.timeout = max_tokens, timeout
        self.url = url or self.URL

    def _key(self) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return key

    @staticmethod
    def _image_block(path: str | Path) -> dict:
        media = mimetypes.guess_type(str(path))[0] or "image/png"
        data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}

    def build_request(self, prompt: str, schema: dict, images: Sequence[str | Path]) -> dict:
        content: list[dict] = [self._image_block(p) for p in images]
        content.append({"type": "text", "text": prompt})
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": content}],
            "tools": [{"name": "report", "description": "Report the structured result.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "report"},
        }

    def complete_json(self, prompt: str, schema: dict, images: Sequence[str | Path] = ()) -> dict:
        body = json.dumps(self.build_request(prompt, schema, images)).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": self._key(),
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Anthropic API error {exc.code}: {exc.read()[:500]!r}") from None
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return block["input"]
        raise RuntimeError("no tool_use block in the API response")


class ReplayClient:
    """Returns pre-recorded outputs, one per call, in order. No model is used."""

    def __init__(self, outputs: Sequence[dict]):
        self._outputs = list(outputs)
        self.calls: list[str] = []

    def complete_json(self, prompt: str, schema: dict, images: Sequence[str | Path] = ()) -> dict:
        self.calls.append(prompt)
        if not self._outputs:
            raise RuntimeError("ReplayClient has no recorded output left")
        return json.loads(json.dumps(self._outputs.pop(0)))


def make_client(backend: str, model: str | None = None) -> LLMClient:
    """Build a backend by name: "claude" (local CLI) or "anthropic" (API)."""
    if backend == "claude":
        return ClaudeCLIClient(model=model or "sonnet")
    if backend == "anthropic":
        return AnthropicAPIClient(model=model)
    raise ValueError(f"unknown backend {backend!r}")
