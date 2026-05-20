"""
Gutenberg block emitters. Each function returns a string of WP block markup
(comment delimiters + inner HTML) suitable for posting via the REST API's
`content` field. Emitting native blocks (rather than raw HTML in one big
Custom-HTML block) is what makes the post editable in WP's block editor —
album thumbnails become real image blocks, lists become real list blocks, etc.

These functions only emit strings. They don't know about the recap data shape.
"""
from __future__ import annotations

import json

from .normalize import htm


def _block(name: str, attrs: dict | None = None, inner: str = "") -> str:
    if attrs:
        attrs_json = json.dumps(attrs, ensure_ascii=False, separators=(",", ":"))
        open_tag = f"<!-- wp:{name} {attrs_json} -->"
    else:
        open_tag = f"<!-- wp:{name} -->"
    return f"{open_tag}\n{inner}\n<!-- /wp:{name} -->"


def paragraph(html_inner: str) -> str:
    return _block("paragraph", None, f"<p>{html_inner}</p>")


def heading(text_or_html: str, level: int = 1, as_html: bool = False) -> str:
    body = text_or_html if as_html else htm(text_or_html)
    return _block(
        "heading",
        {"level": level},
        f'<h{level} class="wp-block-heading">{body}</h{level}>',
    )


def list_block(inner_li_html_list: list[str], ordered: bool = False) -> str:
    items = "\n".join(_block("list-item", None, f"<li>{li}</li>") for li in inner_li_html_list)
    attrs = {"ordered": True} if ordered else None
    tag = "ol" if ordered else "ul"
    return _block("list", attrs, f'<{tag} class="wp-block-list">\n{items}\n</{tag}>')


def image(src: str, alt: str = "", media_id: int | None = None,
          link_url: str | None = None, size_slug: str = "large") -> str:
    """
    Real wp:image block. media_id is set when we've uploaded the file to WP
    Media; hot-linked external URLs leave it None (the editor still treats it
    as an image block — you just can't replace-from-library cleanly).
    """
    attrs: dict = {"sizeSlug": size_slug, "linkDestination": "custom" if link_url else "none"}
    if media_id is not None:
        attrs["id"] = media_id
    img_cls = f' class="wp-image-{media_id}"' if media_id is not None else ""
    img = f'<img src="{htm(src)}" alt="{htm(alt)}"{img_cls}/>'
    if link_url:
        img = f'<a href="{htm(link_url)}">{img}</a>'
    return _block("image", attrs,
                  f'<figure class="wp-block-image size-{size_slug}">{img}</figure>')


def columns(inner_column_blocks: list[str]) -> str:
    inner = "\n".join(inner_column_blocks)
    return _block("columns", None, f'<div class="wp-block-columns">\n{inner}\n</div>')


def column(inner_blocks: list[str] | str) -> str:
    inner = "\n".join(inner_blocks) if isinstance(inner_blocks, (list, tuple)) else inner_blocks
    return _block("column", None, f'<div class="wp-block-column">\n{inner}\n</div>')


def raw_html(html: str) -> str:
    """Escape hatch for arbitrary HTML — e.g. the artist-initials placeholder tile."""
    return _block("html", None, html)


def link_or_text(label_html: str, url: str | None) -> str:
    """Wrap label in <a> if url is truthy, otherwise return the label as-is."""
    if url:
        return f'<a href="{htm(url)}">{label_html}</a>'
    return label_html
