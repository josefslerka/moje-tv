"""Wiki v aplikaci: vykreslí markdown z Obsidianu do HTML.

Umí to, co generátor wiki používá: hlavičku (frontmatter), odkazy [[…]] i v tabulkách,
rámečky > [!info] a odkazy s kotvou na nadpis. Soubory jen čte, nic do nich nepíše.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import quote

from markdown_it import MarkdownIt

WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
CALLOUT = re.compile(r"<blockquote>\s*<p>\[!([\w-]+)\][-+]?[ \t]*([^\n<]*)(\n|</p>)")
ORDER = {"Moje TV": 0, "Rozcestníky": 1, "Zdroje": 2, "Kanály": 3}

_md = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")


def slug(text: str) -> str:
    """Kotva nadpisu. Stejná funkce se používá pro id nadpisu i pro odkaz na něj."""
    return re.sub(r"\s+", "-", text.strip().lower())


def page_url(rel: str, heading: str = "") -> str:
    return "/wiki/" + quote(rel) + (f"#{quote(slug(heading))}" if heading else "")


def resolve(root: Path, rel: str) -> Path | None:
    """Cesta ke stránce, nebo None, když neexistuje nebo míří mimo složku wiki."""
    rel = rel.strip("/") or "Moje TV"
    p = (root / f"{rel}.md").resolve()
    if not p.is_file() or not p.is_relative_to(root.resolve()):
        return None
    return p


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        val = val.strip()
        try:
            meta[key.strip()] = json.loads(val)
        except ValueError:
            meta[key.strip()] = val
    return meta, text[end + 5:]


def _wikilinks(body: str, prefix: str, obsidian: str) -> str:
    def repl(m: re.Match) -> str:
        target, label = (re.split(r"\\?\|", m.group(1), maxsplit=1) + [""])[:2]
        page, _, heading = target.partition("#")
        label = label or heading or page.rsplit("/", 1)[-1]
        label = label.replace("[", "\\[").replace("]", "\\]")
        if page.startswith(prefix + "/"):
            return f"[{label}]({page_url(page[len(prefix) + 1:], heading)})"
        # Stránka mimo wiki (jinde v trezoru): otevře se v Obsidianu.
        return f"[{label}]({obsidian}{quote(page)})"
    return WIKILINK.sub(repl, body)


def _headings(tokens) -> list[dict]:
    toc = []
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            text = tokens[i + 1].content
            t.attrSet("id", slug(text))
            if t.tag in ("h2", "h3"):
                toc.append({"level": int(t.tag[1]), "text": text, "id": slug(text)})
    return toc


def _callouts(out: str) -> str:
    def repl(m: re.Match) -> str:
        kind, title = m.group(1).lower(), m.group(2).strip() or m.group(1).capitalize()
        rest = "<p>" if m.group(3) == "\n" else "<p hidden>"
        return f'<blockquote class="callout callout-{html.escape(kind)}"><p class="callout-title">{title}</p>{rest}'
    return CALLOUT.sub(repl, out).replace("<p hidden></p>", "")


def render(root: Path, rel: str, prefix: str, vault: str, port: int) -> dict | None:
    p = resolve(root, rel)
    if not p:
        return None
    meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
    obsidian = f"obsidian://open?vault={quote(vault)}&file="
    body = _wikilinks(body, prefix, obsidian)
    # Odkazy na tenhle server zapsané plnou adresou (kvůli Obsidianu) vedou v aplikaci na stejný web.
    # Viditelné adresy typu <http://…:8090> zůstanou, ať je jde opsat do Apple TV.
    body = re.sub(rf"\]\(https?://[\w.-]+:{port}(/[^)]*)?\)", lambda m: f"]({m.group(1) or '/'})", body)
    tokens = _md.parse(body)
    toc = _headings(tokens)
    title = next((h["text"] for h in toc if h["level"] == 1), None)
    for i, t in enumerate(tokens):
        if t.type == "heading_open" and t.tag == "h1":
            title = tokens[i + 1].content
            break
    out = _callouts(_md.renderer.render(tokens, _md.options, {}))
    rel = str(p.relative_to(root.resolve()))[:-3]
    return {"path": rel, "title": title or p.stem, "meta": meta, "html": out, "toc": toc,
            "obsidian": obsidian + quote(f"{prefix}/{rel}")}


def tree(root: Path) -> list[dict]:
    """Seznam stránek po složkách pro boční panel."""
    groups: dict[str, list] = {}
    for p in root.rglob("*.md"):
        rel = p.relative_to(root)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        meta, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        groups.setdefault(folder, []).append({"path": str(rel)[:-3], "title": p.stem,
                                              "mine": meta.get("v_mem_seznamu") is True,
                                              "gone": "už není ve výběru" in p.read_text(encoding="utf-8")})
    out = []
    for folder in sorted(groups, key=lambda f: (ORDER.get(f, 9) if f else -1, f)):
        pages = sorted(groups[folder], key=lambda x: (ORDER.get(x["title"], 9), x["title"].lower()))
        out.append({"folder": folder, "pages": pages})
    return out


def search(root: Path, q: str, limit: int = 30) -> list[dict]:
    """Fulltext přes stránky. Shoda v názvu má přednost před shodou v textu."""
    q = q.strip().lower()
    if not q:
        return []
    hits = []
    for p in root.rglob("*.md"):
        _, body = split_frontmatter(p.read_text(encoding="utf-8"))
        body = WIKILINK.sub(lambda m: re.split(r"\\?\|", m.group(1))[-1], body)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
        text = re.sub(r"<[^>]+>|[#>*`|_]|\[![\w-]+\]", " ", text)
        text = re.sub(r"\s+", " ", text)
        in_title = q in p.stem.lower()
        m = re.search(r"\b" + re.escape(q), text.lower())   # od začátku slova, ne „arte“ v „headquarters“
        pos = m.start() if m else -1
        if not in_title and pos < 0:
            continue
        snippet = text[max(0, pos - 60): pos + 90].strip() if pos >= 0 else ""
        hits.append({"path": str(p.relative_to(root))[:-3], "title": p.stem, "snippet": snippet,
                     "score": (0 if in_title else 1, p.stem.lower())})
    hits.sort(key=lambda h: h["score"])
    for h in hits:
        del h["score"]
    return hits[:limit]
