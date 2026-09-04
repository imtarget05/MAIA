"""Document ingestion: raw -> parse -> clean/normalize -> docs with metadata (§4).

Metadata fields (only implemented ones): doc_id, filename, source,
section, page, timestamp, chunk_id (chunk_id added in chunking step).
Uses LlamaIndex readers when available, pure-python fallback otherwise.
"""
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RawDoc:
    text: str
    metadata: dict = field(default_factory=dict)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # strip control chars except \n \t
    text = "".join(c for c in text if c == "\n" or c == "\t" or ord(c) >= 32)
    return text.strip()


def _doc_id_for(path: Path) -> str:
    h = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:12]
    return h


def load_documents(data_dir: str | Path) -> list[RawDoc]:
    """Load .md/.txt/.pdf from data_dir using LlamaIndex if present."""
    data_dir = Path(data_dir)
    docs: list[RawDoc] = []
    if not data_dir.exists():
        return docs

    # Try LlamaIndex SimpleDirectoryReader first
    try:
        from llama_index.core import SimpleDirectoryReader

        reader_docs = SimpleDirectoryReader(
            input_dir=str(data_dir), recursive=True, required_exts=[".md", ".txt", ".pdf"]
        ).load_data()
        for d in reader_docs:
            meta = dict(d.metadata or {})
            fname = meta.get("file_name", "unknown")
            fpath = data_dir / fname if (data_dir / fname).exists() else Path(meta.get("file_path", fname))
            text = _clean(d.text or "")
            if not text:
                continue
            docs.append(
                RawDoc(
                    text=text,
                    metadata={
                        "doc_id": _doc_id_for(fpath),
                        "filename": fname,
                        "source": str(fpath),
                        "section": meta.get("section", ""),
                        "page": str(meta.get("page_label", meta.get("page", ""))),
                        "timestamp": str(int(time.time())),
                    },
                )
            )
        if docs:
            return docs
    except Exception:
        pass

    # Fallback: pure python for .md/.txt (+ pypdf for .pdf)
    for fpath in sorted(data_dir.rglob("*")):
        if not fpath.is_file() or fpath.suffix.lower() not in (".md", ".txt", ".pdf"):
            continue
        try:
            if fpath.suffix.lower() == ".pdf":
                try:
                    from pypdf import PdfReader

                    reader = PdfReader(str(fpath))
                    text = "\n\n".join([(p.extract_text() or "") for p in reader.pages])
                except Exception:
                    continue
            else:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            text = _clean(text)
            if not text:
                continue
            docs.append(
                RawDoc(
                    text=text,
                    metadata={
                        "doc_id": _doc_id_for(fpath),
                        "filename": fpath.name,
                        "source": str(fpath),
                        "section": "",
                        "page": "",
                        "timestamp": str(int(time.time())),
                    },
                )
            )
        except Exception:
            continue
    return docs
