import csv
import io
from pathlib import Path
from typing import Any

SUPPORTED_EXTENSIONS = {".csv", ".md", ".pdf", ".txt"}
MAX_CHARS_PER_FILE = 12000

# Folder documentation is not deal evidence and should never enter a prompt.
IGNORED_FILENAMES = {"readme.md", "readme.txt", "index.md"}


def classify_document(name: str, text: str) -> str:
    haystack = f"{name}\n{text[:3000]}".lower()
    legal_terms = [
        "agreement",
        "assignment",
        "change of control",
        "contract",
        "indemnity",
        "lawsuit",
        "liability",
        "termination",
        "warranty",
    ]
    financial_terms = [
        "balance sheet",
        "burn rate",
        "cash flow",
        "ebitda",
        "expenses",
        "forecast",
        "gross margin",
        "profit",
        "revenue",
    ]
    corporate_terms = [
        "articles",
        "board",
        "cap table",
        "director",
        "shareholder",
        "subsidiary",
    ]

    scores = {
        "legal": sum(term in haystack for term in legal_terms),
        "financial": sum(term in haystack for term in financial_terms),
        "corporate": sum(term in haystack for term in corporate_terms),
    }
    winner, score = max(scores.items(), key=lambda item: item[1])
    return winner if score else "unknown"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_csv(path: Path) -> str:
    rows: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        for row_index, row in enumerate(reader):
            rows.append(" | ".join(cell.strip() for cell in row))
            if row_index >= 100:
                rows.append("[truncated after 100 rows]")
                break
    return "\n".join(rows)


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[PDF extraction unavailable: install pypdf to read PDF data room files]"

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page_index, page in enumerate(reader.pages):
        pages.append(page.extract_text() or "")
        if page_index >= 20:
            pages.append("[truncated after 20 pages]")
            break
    return "\n".join(pages)


def extract_document(path: Path, max_chars: int = MAX_CHARS_PER_FILE) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        text = _read_text(path)
    elif suffix == ".csv":
        text = _read_csv(path)
    elif suffix == ".pdf":
        text = _read_pdf(path)
    else:
        raise ValueError(f"Unsupported data room file type: {suffix}")

    truncated = len(text) > max_chars
    text = text[:max_chars]
    return {
        "file_name": path.name,
        "path": str(path),
        "extension": suffix,
        "classification": classify_document(path.name, text),
        "text_excerpt": text,
        "char_count": len(text),
        "truncated": truncated,
    }


def load_data_room(folder: str | Path = "data_room") -> dict[str, Any]:
    # An empty or bare-dot path would resolve to the working directory and sweep the
    # whole application into a prompt. Treat "no folder" as "no documents".
    if not folder or str(folder).strip() in {"", ".", "./"}:
        return {
            "status": "not_provided",
            "folder": "",
            "documents": [],
            "errors": [],
            "message": "No deal documents were supplied; the audit is statutory-only.",
        }

    root = Path(folder)
    if not root.exists():
        return {
            "status": "not_found",
            "folder": str(root),
            "documents": [],
            "errors": [],
            "message": "Data room folder does not exist yet.",
        }

    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if path.name.lower() in IGNORED_FILENAMES:
            continue
        try:
            documents.append(extract_document(path))
        except Exception as exc:
            errors.append(
                {
                    "file_name": path.name,
                    "path": str(path),
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    return {
        "status": "success" if documents else "empty",
        "folder": str(root),
        "documents": documents,
        "errors": errors,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
    }


def save_uploaded_files(uploaded_files: list[Any], folder: str | Path = "data_room/uploads") -> list[Path]:
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for uploaded_file in uploaded_files:
        safe_name = Path(uploaded_file.name).name
        target = root / safe_name
        target.write_bytes(uploaded_file.getvalue())
        saved.append(target)
    return saved
