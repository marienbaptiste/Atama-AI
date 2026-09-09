"""The Golden Rule gate must catch every violation shape and pass a clean tree (ROADMAP subsystem 0)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.tools import readonly_gate as gate

REPO = Path(__file__).resolve().parents[2]

CLEAN_HTTP = '''
import httpx
from backend import constants

class _GuardTransport(httpx.BaseTransport):
    def __init__(self, inner): self._inner = inner
    def handle_request(self, request): return self._inner.handle_request(request)

class SrsClient:
    def __init__(self, service, token): self._token = token
    def get(self, path, params=None): return {}
    def _throttle(self): pass
'''

CLEAN_FETCHER = '''
from backend.srs.http import SrsClient

def fetch_profile(client: SrsClient):
    return client.get("/user")
'''


def make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


def rules(violations):
    return sorted({v.rule for v in violations})


def test_clean_tree_passes(tmp_path):
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/wanikani.py": CLEAN_FETCHER})
    assert gate.run(root) == []


@pytest.mark.parametrize("body", [
    "import httpx\nfrom backend.srs.http import SrsClient\ndef f(c):\n    return httpx.post('https://api.bunpro.jp/x')\n",
    "from backend.srs.http import SrsClient\ndef f(c):\n    return c._http.request('PUT', '/x')\n",
    "from backend.srs.http import SrsClient\ndef f(c, m):\n    return c._http.request(method=m, url='/x')\n",
    "from backend.srs.http import SrsClient\ndef f(c):\n    return c._http.delete('/x')\n",
    "from backend.srs.http import SrsClient\ndef f(c):\n    return c._http.stream(method='POST', url='/x')\n",
])
def test_rule1_write_calls_in_srs(tmp_path, body):
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro.py": body})
    assert 1 in rules(gate.run(root))


def test_rule1_applies_to_modules_importing_srs_outside_srs(tmp_path):
    body = "from backend.srs.http import SrsClient\nimport httpx\ndef f():\n    httpx.post('https://api.wanikani.com/v2/x')\n"
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/app.py": body})
    assert 1 in rules(gate.run(root))


@pytest.mark.parametrize("name", [
    "set_level", "update_cache", "create_note", "delete_item", "submit_review", "start_assignment",
    "mark_reviewed", "reset_srs", "assign_lesson", "post_answer", "put_thing", "patch_it", "write_state",
])
def test_rule2_setter_shaped_names_rejected_by_name(tmp_path, name):
    body = f"from backend.srs.http import SrsClient\ndef {name}(x):\n    return x  # harmless body, still rejected\n"
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro.py": body})
    assert 2 in rules(gate.run(root))


def test_rule2_attribute_setter_rejected(tmp_path):
    body = "from backend.srs.http import SrsClient\nclass X:\n    def __init__(self):\n        self.update_token = None\n"
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro.py": body})
    assert 2 in rules(gate.run(root))


def test_rule3_http_import_outside_http_py(tmp_path):
    body = "import requests\nfrom backend.srs.http import SrsClient\n"
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro.py": body})
    assert 3 in rules(gate.run(root))


def test_rule4_second_public_method_on_client(tmp_path):
    http_py = CLEAN_HTTP + "\n    def fetch(self, path): return self.get(path)\n"
    root = make_tree(tmp_path, {"backend/srs/http.py": http_py})
    assert 4 in rules(gate.run(root))


def test_rule4_missing_guard_transport(tmp_path):
    http_py = CLEAN_HTTP.replace("class _GuardTransport(httpx.BaseTransport):", "class _Other(httpx.BaseTransport):")
    root = make_tree(tmp_path, {"backend/srs/http.py": http_py})
    assert 4 in rules(gate.run(root))


def test_rule5_fourth_mcp_tool(tmp_path):
    body = '''
    from backend.srs.http import SrsClient
    READ_TOOLS = ("get_review_queue", "get_ghost_reviews", "get_grammar_progress", "add_to_reviews")
    '''
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro_mcp.py": body})
    assert 5 in rules(gate.run(root))


def test_rule5_tool_decorator_name(tmp_path):
    body = '''
    from backend.srs.http import SrsClient
    @server.tool(name="mark_mastered")
    def x(): pass
    '''
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro_mcp.py": body})
    assert 5 in rules(gate.run(root))


def test_rule6_write_scope_string_in_code(tmp_path):
    scope = ":".join(("assignments", "start"))
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/x.py": f"S = '{scope}'\n"})
    assert 6 in rules(gate.run(root))


def test_rule6_allowed_in_doctor_and_docs(tmp_path):
    scope = ":".join(("reviews", "create"))
    root = make_tree(tmp_path, {
        "backend/srs/http.py": CLEAN_HTTP,
        "backend/tools/doctor.py": f"WRITE = ['{scope}']\n",
        "README.md": f"leave {scope} unticked\n",
    })
    assert 6 not in rules(gate.run(root))


@pytest.mark.parametrize("body", [
    "from backend.srs.http import SrsClient\nURL = 'https://example.com/api'\n",
    "from backend.srs.http import SrsClient\ndef make(base_url):\n    return base_url\n",
])
def test_rule7_foreign_url_or_base_url_param(tmp_path, body):
    root = make_tree(tmp_path, {"backend/srs/http.py": CLEAN_HTTP, "backend/srs/bunpro.py": body})
    assert 7 in rules(gate.run(root))


def test_rule7_http_py_must_not_read_env(tmp_path):
    http_py = "import os\n" + CLEAN_HTTP + "\nX = os.environ.get('BUNPRO_ORIGIN')\n"
    root = make_tree(tmp_path, {"backend/srs/http.py": http_py})
    assert 7 in rules(gate.run(root))


def test_real_repo_is_clean():
    violations = gate.run(REPO)
    assert violations == [], "\n".join(map(str, violations))
