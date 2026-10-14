import json
import subprocess
import sys

import pytest

from conftest import EXAMPLES, ROOT
from image2ppt import cli, llm, pipeline, schema
from image2ppt.demo import draw_slide


@pytest.fixture()
def slide(tmp_path):
    png = tmp_path / "slide.png"
    draw_slide(png)
    return png


def test_cli_layout_path_needs_no_model(tmp_path, slide, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a model backend must not be created")
    monkeypatch.setattr(llm, "make_client", boom)
    out = tmp_path / "o.pptx"
    rc = cli.main([str(slide), "-o", str(out), "--layout", str(EXAMPLES / "demo_layout.raw.json"), "--rounds", "0"])
    assert rc == 0 and out.stat().st_size > 0


def test_cli_runs_as_module(tmp_path, slide):
    out = tmp_path / "m.pptx"
    res = subprocess.run([sys.executable, "-m", "image2ppt", str(slide), "-o", str(out), "--layout",
                          str(EXAMPLES / "demo_layout.raw.json"), "--rounds", "0"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(out)


def test_cli_reports_invalid_layout(tmp_path, slide, capsys):
    bad = tmp_path / "bad.raw.json"
    bad.write_text(json.dumps({"image_size": [1200, 640], "background": "FFFFFF", "elements": [{"id": "x", "type": "blob"}]}))
    rc = cli.main([str(slide), "-o", str(tmp_path / "o.pptx"), "--layout", str(bad), "--rounds", "0"])
    assert rc == 1 and "invalid layout" in capsys.readouterr().err


def test_extract_with_replay_client(tmp_path, slide):
    recorded = json.loads((EXAMPLES / "demo_layout.raw.json").read_text())
    client = llm.ReplayClient([recorded])
    lay = pipeline.extract(slide, client)
    assert lay["image_size"] == [1200, 640]
    assert lay["elements"][1]["box"] == [115, 247, 131, 107]  # corners -> [x, y, w, h]
    assert "1200x640" in client.calls[0]


def test_extract_rejects_output_that_breaks_the_schema(slide):
    client = llm.ReplayClient([{"image_size": [1, 1], "background": "FFFFFF", "elements": [{"id": "a", "type": "blob"}]}])
    with pytest.raises(ValueError):
        pipeline.extract(slide, client)


def test_apply_patch_ops():
    lay = {"elements": [{"id": "a", "type": "rect", "box": [0, 0, 10, 10]},
                        {"id": "b", "type": "text", "box": [0, 0, 5, 5], "text": "x"}]}
    patch = {"verdict": "fix", "ops": [
        {"op": "move", "id": "a", "box": [5, 5, 20, 20]},
        {"op": "set", "id": "b", "fields": {"text": "y"}},
        {"op": "add", "element": {"type": "rect", "box": [1, 1, 2, 2]}},
        {"op": "delete", "id": "b"},
        {"op": "move", "id": "missing", "box": [0, 0, 1, 1]},
    ]}
    assert pipeline.apply_patch(lay, patch) == 4
    assert [e["id"] for e in lay["elements"]] == ["a", "x2"]
    assert lay["elements"][0]["box"] == [5, 5, 20, 20]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_anthropic_client_builds_request_and_reads_tool_output(monkeypatch, slide):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        return FakeResponse({"content": [{"type": "tool_use", "name": "report", "input": {"ok": 1}}]})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key-for-test")
    out = llm.AnthropicAPIClient(model="some-model").complete_json("hello", schema.PATCH_SCHEMA, images=[slide])
    assert out == {"ok": 1}
    assert seen["headers"]["x-api-key"] == "dummy-key-for-test"
    body = seen["body"]
    assert body["model"] == "some-model"
    assert body["tool_choice"] == {"type": "tool", "name": "report"}
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert "dummy-key-for-test" not in json.dumps(body)


def test_anthropic_client_requires_key(monkeypatch, slide):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        llm.AnthropicAPIClient().complete_json("hello", schema.PATCH_SCHEMA, images=[slide])


def test_make_client_names():
    assert isinstance(llm.make_client("claude"), llm.ClaudeCLIClient)
    assert isinstance(llm.make_client("anthropic"), llm.AnthropicAPIClient)
    with pytest.raises(ValueError):
        llm.make_client("other")
