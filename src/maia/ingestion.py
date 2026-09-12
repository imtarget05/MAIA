"""Document ingestion: raw -> parse -> clean/normalize -> docs with metadata (§4).

Metadata fields (only implemented ones): doc_id, filename, source,
section, page, timestamp, chunk_id (chunk_id added in chunking step).
Uses LlamaIndex readers when available, pure-python fallback otherwise.

URL sources (notebook-style "paste a link"): fetch_url() downloads one page
(or PDF) into a RawDoc with source=<url>. Chunks carry session_id so links
stay scoped to the session that added them (see pipeline_query.ingest_url).
"""
import hashlib
import html as _html
import ipaddress
import re
import socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RawDoc:
    text: str
    metadata: dict = field(default_factory=dict)


_TONE_START = set("ầấẩẫậằắẳẵặềếểễệìíỉĩịồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"
                  "àáảãạèéẻẽẹòóỏõọ")
_BARE_HORN = set("ăâêôơư")
_BARE_VOWEL_END = _BARE_HORN


def _fix_vn_pdf_spaces(text: str) -> str:
    """Rejoin Vietnamese words split by PDF glyph extraction ("c ầu" -> "cầu").

    pypdf often inserts a space right before a diacritic glyph (diacritics
    are separate glyphs in many PDF fonts). Heuristic: merge a space-separated
    fragment when the word before the space is a short ASCII(-ish) token and
    the fragment starts with a tone-marked or bare horn/breve vowel, or when
    the previous word ends with a bare horn/breve vowel and the fragment
    starts with a tone-marked vowel. Real word boundaries ("tôi Ấy",
    "làm ơn", "Ngô ơi") don't match this pattern.
    """
    def fix(m: "re.Match[str]") -> str:
        prev, nxt = m.group(1), m.group(2)
        short_ascii = len(prev) <= 2 and all(c.isascii() or c in "Đđ" for c in prev)
        starts_marked = nxt[0] in _TONE_START
        starts_horn = nxt[0] in _BARE_HORN
        ends_bare = prev[-1].lower() in _BARE_VOWEL_END
        if (short_ascii and (starts_marked or starts_horn)) or (ends_bare and starts_marked):
            return prev + nxt
        return m.group(0)

    return re.sub(
        r"([A-Za-zÀ-ỹ]+) ([" + "".join(sorted(_TONE_START | _BARE_HORN)) + r"][a-zà-ỹ]*)", fix, text)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # strip control chars except \n \t
    text = "".join(c for c in text if c == "\n" or c == "\t" or ord(c) >= 32)
    return _fix_vn_pdf_spaces(text).strip()


def _doc_id_for(path: Path) -> str:
    """Deterministic, machine-independent document id.

    Old scheme hashed the ABSOLUTE path (``sha1(str(path.resolve()))``), so
    the same repo produced different chunk ids on different machines — and
    the CI golden gates (contact-usability, threshold regression) could never
    match gold_chunk_ids on GitHub runners. New scheme is content-addressed:
    repo-relative path + content digest. Any machine ingesting the same
    files at the same repo-relative location yields identical doc ids, and a
    content edit rotates the id (golden sets track real drift).
    """
    name = path.name
    try:
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
    except Exception:
        digest = ""
    try:
        rel = path.resolve().relative_to(Path(__file__).resolve().parents[2])
    except ValueError:  # file outside the repo — keep the bare name
        rel = Path(name)
    h = hashlib.sha1(f"{rel}|{digest}".encode()).hexdigest()[:12]
    return h


# ---- URL sources (notebook-style "paste a link") ---------------------------

_URL_UA = "MAIA-knowledge-bot/1.0 (+local RAG ingestion)"


def normalize_url(url: str) -> str:
    """Canonical form for dedup: trim, drop fragment, lowercase scheme/host."""
    url = (url or "").strip()
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.lower()
    path = parts.path or ""
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, path, parts.query, ""))


def _host_blocked(host: str) -> str | None:
    """SSRF guard: hostname must resolve only to public IPs. Returns reason or None."""
    if not host:
        return "empty host"
    host = host.strip().lower().rstrip(".")
    # literal IP fast path
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC,
                                       type=socket.SOCK_STREAM)
        except OSError:
            return "DNS resolution failed"
        ips = []
        for fam, _, _, _, sockaddr in infos:
            try:
                ips.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
        if not ips:
            return "DNS resolution failed"
    for ip in ips:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return f"non-public IP ({ip})"
    return None


def validate_url(url: str) -> tuple[str, str | None]:
    """Returns (normalized_url, error). error None means the URL may be fetched."""
    norm = normalize_url(url)
    parts = urllib.parse.urlsplit(norm)
    if parts.scheme not in ("http", "https"):
        return norm, "only http/https URLs are supported"
    if "@" in parts.netloc:  # urlsplit strips userinfo into .hostname; check netloc
        return norm, "blocked host: userinfo not allowed"
    reason = _host_blocked(parts.hostname or "")
    if reason:
        return norm, f"blocked host: {reason}"
    return norm, None


def _download(url: str, timeout: int, max_bytes: int) -> tuple[bytes, str, str]:
    """GET url with size cap. Returns (body, content_type, final_url).

    Redirect targets are re-validated so a public URL can't bounce us to
    an internal address (DNS-rebinding between check and fetch remains a
    known residual risk for a local app).
    """
    req = urllib.request.Request(url, headers={"User-Agent": _URL_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
        if normalize_url(final) != normalize_url(url):
            _, err = validate_url(final)
            if err:
                raise ValueError(f"redirect {err}")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        body = b""
        while len(body) < max_bytes + 1:
            buf = resp.read(min(65536, max_bytes + 1 - len(body)))
            if not buf:
                break
            body += buf
        if len(body) > max_bytes:
            raise ValueError(f"page exceeds {max_bytes} bytes")
        return body, ctype, final


def _strip_html_fallback(html_text: str) -> tuple[str, str]:
    """Stdlib tag-stripping used when trafilatura is absent (offline/tests)."""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if m:
        title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    text = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ",
                  html_text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(re.sub(r"\s+", " ", text)).strip()
    return title, text


def _extract_jsonld(html_text: str) -> str:
    """Flatten JSON-LD structured data (name, description, offers/price...).

    E-commerce pages (e.g. Apple Store) render prices client-side but keep
    them in <script type="application/ld+json"> — trafilatura drops these,
    so we append the useful key/value facts to the extracted text.
    """
    import json as _json

    facts: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            name = node.get("name") or node.get("title")
            price = node.get("price") or node.get("lowPrice") or node.get("highPrice")
            if name and price is not None:
                currency = node.get("priceCurrency", "")
                facts.append(f"{name}: {price} {currency}".strip())
            elif name and node.get("description"):
                facts.append(f"{name}: {node['description']}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.IGNORECASE | re.DOTALL,
    ):
        try:
            walk(_json.loads(m.group(1)))
        except Exception:
            continue
    # Inline JS state blobs (e.g. Apple Store): extract price amounts
    # ("amount":"69.999.000đ", "seoPrice":69999000.00) as generic facts.
    for m in re.finditer(r'"amount"\s*:\s*"([\d][\d.,\s]*\s*[đ$€]|[\d][\d.,\s]*)"', html_text):
        facts.append(f"Giá: {m.group(1).strip()}")
    seen_amounts = set()
    deduped = []
    for f in facts:
        if f.startswith("Giá: "):
            if f in seen_amounts:
                continue
            seen_amounts.add(f)
        deduped.append(f)
    return "\n".join(dict.fromkeys(deduped))  # dedup, keep order


def _extract_html(body: bytes, url: str) -> tuple[str, str]:
    """(title, text) via trafilatura, falling back to stdlib stripping."""
    html_text = body.decode("utf-8", errors="ignore")
    jsonld = _extract_jsonld(html_text)
    try:
        from trafilatura import extract, extract_metadata

        text = extract(html_text, include_comments=False, include_tables=True) or ""
        title = ""
        try:
            meta = extract_metadata(html_text)
            title = (getattr(meta, "title", "") or "").strip()
        except Exception:
            pass
        if text.strip():
            return title, _clean(text + ("\n" + jsonld if jsonld else ""))
    except ImportError:
        pass
    except Exception:
        pass
    title, text = _strip_html_fallback(html_text)
    return title, _clean(text + ("\n" + jsonld if jsonld else ""))


def _extract_pdf_bytes(body: bytes) -> str:
    import io

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        return _clean("\n\n".join([(p.extract_text() or "") for p in reader.pages]))
    except Exception:
        return ""


def _looks_like_pdf(url: str, content_type: str) -> bool:
    return content_type == "application/pdf" or urllib.parse.urlsplit(url).path.lower().endswith(".pdf")


def fetch_url(url: str, session_id: str = "") -> RawDoc:
    """Fetch one page/PDF into a RawDoc. Raises ValueError on any refusal.

    doc_id = sha1(session_id + "|" + normalized_url): same URL re-added in
    the same session overwrites (dedup); other sessions get their own copy.
    """
    from .config import settings as _s

    norm, err = validate_url(url)
    if err:
        raise ValueError(err)
    body, ctype, final = _download(norm, timeout=_s.URL_FETCH_TIMEOUT,
                                   max_bytes=_s.URL_MAX_BYTES)
    if _looks_like_pdf(final, ctype):
        title = urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or "document.pdf"
        text = _extract_pdf_bytes(body)
    elif ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
        raise ValueError(f"unsupported content type: {ctype or 'unknown'}")
    else:
        title, text = _extract_html(body, final)
    if not text:
        raise ValueError("no readable text extracted")
    host = urllib.parse.urlsplit(final).hostname or "web"
    filename = (title[:80] + f" — {host}" if title else host)[:120]
    doc_id = hashlib.sha1(f"{session_id}|{norm}".encode()).hexdigest()[:12]
    return RawDoc(
        text=text,
        metadata={
            "doc_id": doc_id,
            "filename": filename,
            "source": final,
            "section": "",
            "page": "",
            "timestamp": str(int(time.time())),
            "origin": "url",
        },
    )


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
        # Group pages by file: a PDF arrives as one LlamaIndex doc per page,
        # but every page shares the same doc_id — chunk ids would collide and
        # pages overwrite each other at upsert time (massive data loss).
        by_file: dict[str, dict] = {}
        for d in reader_docs:
            meta = dict(d.metadata or {})
            fname = meta.get("file_name", "unknown")
            fpath = data_dir / fname if (data_dir / fname).exists() else Path(meta.get("file_path", fname))
            text = _clean(d.text or "")
            if not text:
                continue
            entry = by_file.setdefault(str(fpath), {
                "fpath": fpath, "fname": fname, "texts": [], "pages": []})
            entry["texts"].append(text)
            entry["pages"].append(str(meta.get("page_label", meta.get("page", ""))))
        for entry in by_file.values():
            fpath, fname = entry["fpath"], entry["fname"]
            docs.append(
                RawDoc(
                    text="\n\n".join(entry["texts"]),
                    metadata={
                        "doc_id": _doc_id_for(fpath),
                        "filename": fname,
                        "source": str(fpath),
                        "section": "",
                        "page": ", ".join(p for p in entry["pages"] if p and p != "None"),
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
