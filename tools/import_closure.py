"""Liệt kê external deps cần để `import maia.api` KHÔNG NỔI, theo transitive closure.

Bài học từ lần deploy MAIA thất bại: quét AST của đúng một file (api.py) là
KHÔNG ĐỦ. api.py chỉ ghi `from maia.auth import ...`, còn passlib nằm bên
trong auth.py — nên image build thành công rồi mới crash lúc uvicorn import,
với ModuleNotFoundError chỉ chỉ ra package, chứ không chỉ ra ai cần nó.

Và KHÔNG phải import nào cũng quan trọng như nhau. Chỉ import ở MODULE SCOPE mới
chạy lúc uvicorn khởi động app. Import nằm trong thân hàm chỉ chạy khi hàm đó
được gọi — ví dụ Reranker() chỉ chạy khi /ready được gọi, nên thiếu nó vẫn để
app đứng vững.

Vì vậy script tách hai nhóm:
  REQUIRED -> phải cài, không có thì app không import được
  LAZY    -> chỉ cần khi endpoint gọi tới; /ready và /query degrade rõ ràng
             thay vì kéo cả app xuống

Đây là cơ sở để chọn image nhẹ mà vẫn trung thực về những gì thực sự chạy.
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
HEAVY = {"torch", "onnxruntime", "sentence_transformers", "transformers", "faiss"}


def module_path(mod: str):
    rel = mod.replace(".", "/")
    for candidate in (SRC / f"{rel}.py", SRC / rel / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def imports_of(path, module_scope_only: bool, package: str):
    """Yield import names, relative imports đã resolve về absolute.

    `from .agent import X` phải trở thành `maia.agent`, nếu không node.module là
    "agent" và bị tính nhầm là package ngoài — đó là lý do danh sách đầu ra
    toàn agent/config/pipeline, tức là chính các module của MAIA.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return

    for node in tree.body:  # chỉ duyệt body cấp module
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                base = package.split(".")
                # level=1 -> package của module hiện tại; level=2 -> bố của nó
                trimmed = base[: len(base) - (node.level - 1)]
                yield ".".join([*trimmed, node.module])
            else:
                yield node.module


def collect(module_scope_only: bool):
    seen, external, unresolved = set(), set(), set()
    queue = ["maia.api"]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = module_path(mod)
        if path is None:
            unresolved.add(mod)
            continue
        # package của module: bỏ `.py`; `__init__` nằm ngay tại package
        is_pkg = path.name == "__init__.py"
        package = mod if is_pkg else mod.rsplit(".", 1)[0]
        if is_pkg and "." not in mod:
            package = ""
        for imported in imports_of(path, module_scope_only, package):
            head = imported.split(".", 1)[0]
            if head == "maia":
                queue.append(imported)
            elif head not in STDLIB and head != "__future__":
                external.add(head)
    return seen, external, unresolved


STDLIB = set(sys.stdlib_module_names)


def main() -> int:
    modules, required, unresolved = collect(module_scope_only=True)
    _, everything, _ = collect(module_scope_only=False)
    lazy_only = everything - required

    print(f"maia modules walked : {len(modules)}")
    print()
    print("REQUIRED AT IMPORT TIME (app will not start without these):")
    for name in sorted(required):
        print(f"  {name}")
    print()
    print(f"LAZY-ONLY ({len(lazy_only)}, needed only when an endpoint calls them):")
    for name in sorted(lazy_only):
        print(f"  {name}{'  <-- HEAVY' if name in HEAVY else ''}")
    print()
    print(f"heavy libs required at import time: {sorted(required & HEAVY) or 'NONE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

