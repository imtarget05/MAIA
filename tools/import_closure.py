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


CLASSES_THAT_DEFER_IMPORTS = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


def imports_of(path, include_deferred: bool, package: str):
    """Yield absolute import names.

    `include_deferred=False` giữ lại đúng các import chạy lúc uvicorn khởi
    động app; True thì lấy thêm cả import bên trong hàm.

    Bốn lỗi đã tốn thời gian ở đây, tất cả đều là "quét hụt import":

    1. Chỉ đọc `tree.body` bỏ sót import nằm trong `try:` / `if:` ở CẤP MODULE.
       Chúng vẫn chạy lúc import — chỉ khác là được bọc try/except. Đây
       chính là lý do langgraph biến mất khỏi danh sách rồi app crash.
    2. Dùng `ast.walk` thì ngược lại quá tay: nó lặn vào thân hàm, nên Reranker
       và toàn bộ RAG stack trông như bắt buộc lúc khởi động, và image sẽ
       phình lên hàng GB.
    3. `from .agent import X` phải resolve thành `maia.agent`, nếu không node.module
       là "agent" và bị tính nhầm là package của bên thứ ba.
    4. Duyệt cả class body cũng quá tay theo cùng lý do.

    Vì vậy: đi toàn bộ cây, nhưng KHÔNG lặn vào thân hàm/lớp.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return

    def resolve(node) -> str:
        if not node.level:
            return node.module
        base = package.split(".") if package else []
        trimmed = base[: len(base) - (node.level - 1)] if node.level > 1 else base
        return ".".join([*trimmed, node.module])

    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield resolve(node)
        elif isinstance(node, CLASSES_THAT_DEFER_IMPORTS):
            if include_deferred:
                # iter_child_nodes chứ không phải node.body: Lambda.body là
                # một ast.Call chứ không phải list, nên extend() sẽ vỡ.
                stack.extend(ast.iter_child_nodes(node))
        else:
            # if / try / with / for ở cấp module: vẫn là import-time.
            stack.extend(ast.iter_child_nodes(node))


def ancestors(mod: str):
    """Các package cha của `mod`, từ sâu về nông.

    Import `maia.agent.mcp_dispatch` buộc phải chạy `maia/__init__.py` rồi
    `maia/agent/__init__.py` TRƯỚC khi module con được nạp. Bỏ qua chuỗi này
    là lý do langgraph không xuất hiện: `maia/agent/__init__.py` dòng 6 import
    `.langgraph_agent`, và đó mới là nơi langgraph thực sự được cần.

    Đây là loại lỗi mà chỉ lộ ra khi deploy thật — mọi phân tích tĩnh chỉ đọc
    file được import trực tiếp thì đều bỏ lọt.
    """
    parts = mod.split(".")
    for depth in range(len(parts) - 1, 0, -1):
        yield ".".join(parts[:depth])


def collect(include_deferred: bool):
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
        # package cha phải được nạp trước, và `__init__.py` của nó cũng vậy
        for parent in ancestors(mod):
            queue.append(parent)
        # package của module: bỏ `.py`; `__init__` nằm ngay tại package
        is_pkg = path.name == "__init__.py"
        package = mod if is_pkg else mod.rsplit(".", 1)[0]
        if is_pkg and "." not in mod:
            package = ""
        for imported in imports_of(path, include_deferred, package):
            head = imported.split(".", 1)[0]
            if head == "maia":
                queue.append(imported)
            elif head not in STDLIB and head != "__future__":
                external.add(head)
    return seen, external, unresolved


STDLIB = set(sys.stdlib_module_names)


def main() -> int:
    modules, required, unresolved = collect(include_deferred=False)
    _, everything, _ = collect(include_deferred=True)
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

