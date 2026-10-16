#!/usr/bin/env python3
"""Build the slide list in index.html from slides.toml.

Usage
    python build.py            rebuild index.html from slides.toml
    python build.py --add      first add starter entries for PDFs not yet in slides.toml
    python build.py --check    validate only (exit status 1 on problems)
    python build.py --serve    rebuild, then preview at http://localhost:8000

Needs Python 3.8+ and nothing else. If the optional `pypdf` package is
installed, --add reads each new PDF's title from its metadata.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "slides.toml"
PAGE = ROOT / "index.html"
VERSIONED_ASSETS = ("assets/style.css", "assets/viewer.js")
SKIP_DIRS = {"assets", ".git", ".github", "node_modules", "_site"}
FIELDS = {"id", "group", "number", "label", "title", "subtitle", "date", "file", "default"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
DEFAULT_GROUP = "Labs"

DOC_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 '
    '2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg>'
)


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------
# Reading slides.toml
# --------------------------------------------------------------------------

def read_config(path: Path) -> list[dict]:
    if not path.exists():
        raise BuildError(f"{path.name} not found next to build.py")
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        tomllib = None
    try:
        data = tomllib.loads(text) if tomllib else parse_simple_toml(text)
    except Exception as error:  # tomllib.TOMLDecodeError or our own message
        raise BuildError(f"{path.name}: {error}") from None
    decks = data.get("deck", [])
    if not isinstance(decks, list) or not decks:
        raise BuildError(f"{path.name} has no [[deck]] entries")
    return decks


def parse_simple_toml(text: str) -> dict:
    """Read the small TOML subset used by slides.toml (for Python < 3.11)."""
    data: dict = {}
    table = data
    for number, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        header = re.fullmatch(r"\[\[\s*([A-Za-z0-9_-]+)\s*\]\]", line)
        if header:
            table = {}
            data.setdefault(header.group(1), []).append(table)
            continue
        pair = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if not pair:
            raise ValueError(f"line {number}: cannot read {raw.strip()!r}")
        table[pair.group(1)] = parse_value(pair.group(2).strip(), number)
    return data


def strip_comment(line: str) -> str:
    quote_char = None
    escaped = False
    for index, char in enumerate(line):
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\" and quote_char == '"':
                escaped = True
            elif char == quote_char:
                quote_char = None
        elif char in "\"'":
            quote_char = char
        elif char == "#":
            return line[:index]
    return line


def parse_value(value: str, line: int):
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return json.loads(value)
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.date.fromisoformat(value)
    raise ValueError(f"line {line}: unsupported value {value!r}")


# --------------------------------------------------------------------------
# Checking the decks
# --------------------------------------------------------------------------

class Deck:
    def __init__(self, raw: dict, position: int):
        where = f"deck #{position}"
        unknown = set(raw) - FIELDS
        if unknown:
            raise BuildError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        for field in ("id", "title", "file"):
            if not str(raw.get(field, "")).strip():
                raise BuildError(f"{where}: '{field}' is required")

        self.id = str(raw["id"]).strip()
        where = f"deck '{self.id}'"
        if not ID_PATTERN.match(self.id):
            raise BuildError(f"{where}: id may only use letters, digits and '-'")
        self.title = str(raw["title"]).strip()
        self.subtitle = str(raw.get("subtitle", "")).strip()
        self.group = str(raw.get("group", DEFAULT_GROUP)).strip() or DEFAULT_GROUP
        self.file = str(raw["file"]).strip().replace("\\", "/")
        self.default = raw.get("default") is True

        number = raw.get("number")
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 0):
            raise BuildError(f"{where}: 'number' must be a whole number")
        self.number = number

        date = raw.get("date")
        if isinstance(date, dt.datetime):
            date = date.date()
        elif isinstance(date, str) and date.strip():
            try:
                date = dt.date.fromisoformat(date.strip())
            except ValueError:
                raise BuildError(f"{where}: 'date' must look like 2026-10-05") from None
        elif date is not None and not isinstance(date, dt.date):
            raise BuildError(f"{where}: 'date' must look like 2026-10-05")
        self.date = date or None

        label = str(raw.get("label", "")).strip()
        if not label:
            if self.number is not None:
                noun = self.group[:-1] if self.group.endswith("s") else self.group
                label = f"{noun} {self.number}"
            else:
                label = self.group
        self.label = label

    @property
    def kicker(self) -> str:
        if self.date:
            return f"{self.label} · {self.date:%B} {self.date.day}"
        return self.label

    @property
    def href(self) -> str:
        return quote(self.file, safe="/")


def load_decks() -> list[Deck]:
    decks = [Deck(raw, position) for position, raw in enumerate(read_config(CONFIG), 1)]
    seen = set()
    for deck in decks:
        if deck.id in seen:
            raise BuildError(f"id '{deck.id}' is used twice")
        seen.add(deck.id)
    if sum(deck.default for deck in decks) > 1:
        raise BuildError("only one deck can set default = true")
    return decks


def missing_files(decks: list[Deck]) -> list[str]:
    return [deck.file for deck in decks if not (ROOT / deck.file).is_file()]


def unlisted_pdfs(decks: list[Deck]) -> list[Path]:
    listed = {deck.file.lower() for deck in decks}
    found = []
    for path in sorted(ROOT.rglob("*.pdf")):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        if relative.as_posix().lower() not in listed:
            found.append(path)
    return found


def default_deck(decks: list[Deck]) -> Deck:
    chosen = next((deck for deck in decks if deck.default), None)
    if chosen:
        return chosen
    first_group = decks[0].group
    return [deck for deck in decks if deck.group == first_group][-1]


# --------------------------------------------------------------------------
# Writing index.html
# --------------------------------------------------------------------------

def esc(text: str) -> str:
    return html.escape(text, quote=True)


def deck_list_html(decks: list[Deck], current: Deck) -> list[str]:
    groups: dict[str, list[Deck]] = {}
    for deck in decks:
        groups.setdefault(deck.group, []).append(deck)

    lines: list[str] = []
    for group, items in groups.items():
        tag = "ol" if all(deck.number is not None for deck in items) else "ul"
        lines.append(f'<h2 class="list-label">{esc(group)}</h2>')
        lines.append(f'<{tag} class="deck-list">')
        for deck in items:
            attributes = f'class="deck" href="{esc(deck.href)}" data-id="{esc(deck.id)}" data-kicker="{esc(deck.kicker)}"'
            if deck is current:
                attributes += " data-default"
            if deck.number is not None:
                marker = f'<span class="deck-num">{deck.number:02d}</span>'
            else:
                marker = f'<span class="deck-num" aria-hidden="true">{DOC_ICON}</span>'
            subtitle = f'<span class="deck-sub">{esc(deck.subtitle)}</span>' if deck.subtitle else ""
            lines += [
                "  <li>",
                f"    <a {attributes}>",
                f"      {marker}",
                f'      <span><span class="deck-title">{esc(deck.title)}</span>{subtitle}</span>',
                "    </a>",
                "  </li>",
            ]
        lines.append(f"</{tag}>")
    return lines


def viewer_heading_html(current: Deck) -> list[str]:
    return [
        '<div class="viewer-heading">',
        f'  <p class="viewer-kicker" id="viewer-kicker">{esc(current.kicker)}</p>',
        f'  <h2 class="viewer-title" id="viewer-title">{esc(current.title)}</h2>',
        "</div>",
    ]


def replace_region(page: str, name: str, body: list[str]) -> str:
    pattern = re.compile(rf"(?P<indent>[ \t]*)<!-- {name}:start -->.*?<!-- {name}:end -->", re.S)
    match = pattern.search(page)
    if not match:
        raise BuildError(f"index.html is missing the <!-- {name}:start --> / <!-- {name}:end --> markers")
    indent = match.group("indent")
    lines = [f"{indent}<!-- {name}:start -->"]
    lines += [f"{indent}{line}" if line else "" for line in body]
    lines.append(f"{indent}<!-- {name}:end -->")
    return page[: match.start()] + "\n".join(lines) + page[match.end():]


def stamp_assets(page: str) -> str:
    """Add ?v=<hash> to CSS/JS links so browsers pick up changes right away."""
    for asset in VERSIONED_ASSETS:
        path = ROOT / asset
        if not path.is_file():
            raise BuildError(f"{asset} is missing")
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
        page = re.sub(rf'{re.escape(asset)}(\?v=[0-9a-f]*)?"', f'{asset}?v={digest}"', page)
    return page


def build(decks: list[Deck]) -> Deck:
    with open(PAGE, encoding="utf-8", newline="") as handle:
        original = handle.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    page = original.replace("\r\n", "\n")

    current = default_deck(decks)
    page = replace_region(page, "viewer", viewer_heading_html(current))
    page = replace_region(page, "decks", deck_list_html(decks, current))
    page = stamp_assets(page)

    page = page.replace("\n", newline)
    if page != original:
        with open(PAGE, "w", encoding="utf-8", newline="") as handle:
            handle.write(page)
    return current


# --------------------------------------------------------------------------
# --add: starter entries for new PDFs
# --------------------------------------------------------------------------

def guess_details(path: Path) -> tuple[str, str]:
    """Title and subtitle from the PDF's metadata if pypdf is available."""
    raw = ""
    try:
        from pypdf import PdfReader

        metadata = PdfReader(str(path)).metadata
        raw = (metadata.title or "") if metadata else ""
    except Exception:
        raw = ""
    lab_prefix = re.compile(r"^(?:CSCI\s*\d+\s*[—–:-]\s*)?Lab\s*\d+\s*[—–:-]\s*", re.I)
    if raw.strip():
        head, _, tail = raw.partition(" - ")
        title = lab_prefix.sub("", head.strip())
        subtitle = re.sub(r"\s{2,}", ", ", lab_prefix.sub("", tail.strip()))
        if subtitle:
            subtitle = subtitle[0].upper() + subtitle[1:]
        return title or path.stem, subtitle
    title = re.sub(r"[\s_-]*slides?$", "", path.stem, flags=re.I).strip(" -_")
    return title or path.stem, ""


def starter_entry(path: Path, used_ids: set[str]) -> tuple[str, str]:
    relative = path.relative_to(ROOT).as_posix()
    match = re.search(r"\blab\s*0*(\d+)", path.stem, re.I)
    number = int(match.group(1)) if match else None
    group = DEFAULT_GROUP if number is not None else "Resources"
    base = f"lab{number}" if number is not None else re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-") or "deck"
    deck_id, suffix = base, 2
    while deck_id in used_ids:
        deck_id, suffix = f"{base}-{suffix}", suffix + 1
    used_ids.add(deck_id)
    title, subtitle = guess_details(path)

    lines = ["[[deck]]", f"id = {json.dumps(deck_id)}", f"group = {json.dumps(group)}"]
    if number is not None:
        lines.append(f"number = {number}")
    lines.append(f"title = {json.dumps(title, ensure_ascii=False)}")
    lines.append(f"subtitle = {json.dumps(subtitle, ensure_ascii=False)}")
    lines.append("# date = YYYY-MM-DD")
    lines.append(f"file = {json.dumps(relative, ensure_ascii=False)}")
    return group, "\n".join(lines)


def add_new_pdfs(decks: list[Deck]) -> list[str]:
    new = unlisted_pdfs(decks)
    if not new:
        return []
    with open(CONFIG, encoding="utf-8", newline="") as handle:
        original = handle.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    used = {deck.id for deck in decks}
    added = []
    for path in new:
        group, entry = starter_entry(path, used)
        text = insert_entry(text, group, entry)
        added.append(path.relative_to(ROOT).as_posix())
    with open(CONFIG, "w", encoding="utf-8", newline="") as handle:
        handle.write(text.replace("\n", newline))
    return added


def insert_entry(text: str, group: str, entry: str) -> str:
    """Place the entry after the last deck of the same group (or at the end)."""
    starts = [m.start() for m in re.finditer(r"^\[\[deck\]\]", text, re.M)]
    insert_at = None
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        found = re.search(r"^\s*group\s*=\s*[\"'](.*?)[\"']", text[start:end], re.M)
        if (found.group(1) if found else DEFAULT_GROUP) == group:
            insert_at = end
    if insert_at is None or insert_at == len(text):
        return text.rstrip("\n") + "\n\n" + entry + "\n"
    return text[:insert_at].rstrip("\n") + "\n\n" + entry + "\n\n" + text[insert_at:]


# --------------------------------------------------------------------------
# --serve: local preview
# --------------------------------------------------------------------------

def serve(port: int) -> None:
    import functools
    import http.server
    import webbrowser

    class Handler(http.server.SimpleHTTPRequestHandler):
        # Windows can map .js to text/plain, which browsers refuse for modules.
        extensions_map = {
            **http.server.SimpleHTTPRequestHandler.extensions_map,
            ".js": "text/javascript",
            ".mjs": "text/javascript",
            ".css": "text/css",
            ".pdf": "application/pdf",
            ".html": "text/html",
        }

        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=str(ROOT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        url = f"http://localhost:{port}/"
        print(f"Previewing at {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()


# --------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Build the slide list in index.html from slides.toml.")
    parser.add_argument("--add", action="store_true", help="add starter entries for PDFs not in slides.toml")
    parser.add_argument("--check", action="store_true", help="validate only, do not write files")
    parser.add_argument("--serve", action="store_true", help="preview the page locally after building")
    parser.add_argument("--port", type=int, default=8000, help="port for --serve (default 8000)")
    args = parser.parse_args()

    try:
        decks = load_decks()
        if args.add and not args.check:
            added = add_new_pdfs(decks)
            for name in added:
                print(f"Added to slides.toml: {name}")
            if added:
                print("Check the new entries in slides.toml (title, subtitle, date).")
                decks = load_decks()

        missing = missing_files(decks)
        for name in missing:
            print(f"Error: file not found: {name}")
        for path in unlisted_pdfs(decks):
            print(f"Note: {path.relative_to(ROOT).as_posix()} is not in slides.toml "
                  f"(run `python build.py --add` to list it)")
        if missing:
            return 1

        if args.check:
            print(f"OK: {len(decks)} decks in slides.toml")
            return 0

        current = build(decks)
        counts = {}
        for deck in decks:
            counts[deck.group] = counts.get(deck.group, 0) + 1
        summary = ", ".join(f"{group}: {count}" for group, count in counts.items())
        print(f"index.html updated ({summary}). Opens on: {current.label} - {current.title}")
    except BuildError as error:
        print(f"Error: {error}")
        return 1

    if args.serve:
        serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
