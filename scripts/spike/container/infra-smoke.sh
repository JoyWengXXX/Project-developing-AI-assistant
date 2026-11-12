#!/bin/sh
# 第二階段切片 1 spike：拋棄式容器內的依賴煙霧測試（venv 建在 /tmp，不寫入專案；容器以 --rm 結束即消失）。
# 用法：infra-smoke.sh <broker URL，例如 redis://redis:6379/0>
# 目的：確認 cp312 manylinux wheel 可安裝（映像沒有編譯器）、可 import，並以 C# 程式碼解析成功；redis-py PING 與 Celery broker 連線。
broker="$1"
export HOME=/tmp PIP_CACHE_DIR=/tmp/pipcache UV_CACHE_DIR=/tmp/uvcache PIP_DISABLE_PIP_VERSION_CHECK=1
echo "python: $(python --version) / $(python -c 'import sysconfig;print(sysconfig.get_platform())')"
echo "glibc: $(ldd --version 2>&1 | head -1)"
echo "compiler present: $(command -v gcc cc 2>/dev/null || echo none)"

echo "--- (A) python -m venv + pip install --only-binary=:all:"
python -m venv /tmp/venv-pip || exit 1
/tmp/venv-pip/bin/pip install --only-binary=:all: tree-sitter tree-sitter-c-sharp 'celery[redis]' > /tmp/pip.log 2>&1
echo "pip install exit=$?"
grep -E 'Downloading|Successfully|ERROR|error' /tmp/pip.log | sed 's/ (.*//' | head -40

echo "--- (B) uv pip install（uv 以 pip 安裝在另一個 venv）"
python -m venv /tmp/venv-uv && /tmp/venv-uv/bin/pip install --quiet uv 2>&1 | tail -1
/tmp/venv-uv/bin/uv --version
/tmp/venv-uv/bin/uv venv --quiet /tmp/venv-uv-target
/tmp/venv-uv/bin/uv pip install --python /tmp/venv-uv-target/bin/python --only-binary :all: tree-sitter tree-sitter-c-sharp 'celery[redis]' 2>&1 | tail -25
echo "--- 已安裝版本（uv 環境）"
/tmp/venv-uv/bin/uv pip list --python /tmp/venv-uv-target/bin/python 2>&1 | grep -iE '^(tree-sitter|tree_sitter|celery|kombu|redis|billiard|vine|amqp)' 

for py in /tmp/venv-pip/bin/python /tmp/venv-uv-target/bin/python; do
echo "--- import 與 C# 解析（$py）"
$py - "$broker" <<'PY'
import sys
from importlib.metadata import version

import tree_sitter_c_sharp as tscs
from tree_sitter import Language, Parser

print("versions:", {n: version(n) for n in ("tree-sitter", "tree-sitter-c-sharp", "celery", "kombu", "redis")})
lang = Language(tscs.language())
print("language abi_version:", getattr(lang, "abi_version", "n/a"), "| name:", getattr(lang, "name", "n/a"))
src = "namespace Demo;\npublic partial class Greeter<T>\n{\n    public string Hello(string name) => $\"hi {name}\";\n}\n#if DEBUG\nclass Dbg {}\n#endif\n".encode()
tree = Parser(lang).parse(src)
root = tree.root_node
print("root:", root.type, "has_error:", root.has_error)
def walk(n, out):
    if n.type in ("file_scoped_namespace_declaration", "class_declaration", "method_declaration"):
        name = n.child_by_field_name("name")
        out.append((n.type, name.text.decode() if name else None, n.start_point))
    for c in n.children:
        walk(c, out)
out = []
walk(root, out)
print("declarations:", out)
bad = Parser(lang).parse(b"class A { void M( { } }")
print("syntax error case has_error:", bad.root_node.has_error)

import redis
r = redis.Redis.from_url(sys.argv[1], socket_connect_timeout=5)
print("redis-py PING:", r.ping(), "| server:", r.info("server")["redis_version"], "| maxmemory-policy:", r.config_get("maxmemory-policy"))
from celery import Celery
app = Celery("spike", broker=sys.argv[1])
with app.connection_for_write() as conn:
    conn.ensure_connection(max_retries=2)
    print("celery broker connection:", conn.as_uri(), "connected=", conn.connected)
PY
done
