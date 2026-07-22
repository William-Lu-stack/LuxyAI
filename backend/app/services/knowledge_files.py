"""安全提取上传到知识库的文件文本。

本模块不负责持久化或调用 LLM，只校验文件类型、在进程内抽取文本并返回
标准化文档类型，后续分块与向量化策略由知识库服务统一处理。
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from fastapi import HTTPException


KNOWLEDGE_FILE_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".odt", ".csv", ".md", ".markdown",
    ".txt", ".log", ".json", ".yaml", ".yml", ".html", ".htm", ".xml", ".rtf",
}


class _VisibleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag.lower() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _xml_text(xml_bytes: bytes, include_values: bool = False) -> list[str]:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []
    accepted = {"t", "text", "p", "span"}
    if include_values:
        accepted.update({"v", "f"})
    return [
        node.text.strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].lower() in accepted and node.text and node.text.strip()
    ]


def _extract_open_document(data: bytes, extension: str) -> str:
    selectors = {
        ".docx": ("word/", ("document.xml", "header", "footer", "footnotes.xml", "endnotes.xml"), False),
        ".pptx": ("ppt/slides/", ("slide",), False),
        ".xlsx": ("xl/", ("sharedStrings.xml", "worksheets/", "comments"), True),
        ".odt": ("", ("content.xml",), False),
    }
    prefix, markers, include_values = selectors[extension]
    parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in sorted(archive.namelist()):
                if not name.startswith(prefix) or not name.endswith(".xml"):
                    continue
                if not any(marker in name for marker in markers):
                    continue
                if archive.getinfo(name).file_size <= 12 * 1024 * 1024:
                    parts.extend(_xml_text(archive.read(name), include_values=include_values))
    except (zipfile.BadZipFile, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Office 文档结构无效：{type(exc).__name__}") from exc
    return "\n".join(parts)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="PDF 解析组件未安装，请使用更新后的项目镜像") from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise HTTPException(status_code=422, detail="PDF 已加密，无法抽取文本") from exc
        pages: list[str] = []
        for page in reader.pages[:500]:
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"PDF 文本抽取失败：{type(exc).__name__}: {str(exc)[:120]}") from exc


def extract_knowledge_file(data: bytes, filename: str) -> tuple[str, str]:
    """Return normalized text and document type for an allowlisted upload."""
    extension = Path(filename).suffix.lower()
    if extension not in KNOWLEDGE_FILE_EXTENSIONS:
        supported = ", ".join(sorted(KNOWLEDGE_FILE_EXTENSIONS))
        raise HTTPException(status_code=415, detail=f"不支持 {extension or '无扩展名'} 文件；支持：{supported}")
    if extension == ".pdf":
        content = _extract_pdf(data)
    elif extension in {".docx", ".pptx", ".xlsx", ".odt"}:
        content = _extract_open_document(data, extension)
    else:
        content = _decode_text(data)
        if extension in {".html", ".htm"}:
            parser = _VisibleHTMLParser()
            parser.feed(content)
            content = " ".join(parser.parts)
        elif extension == ".rtf":
            content = re.sub(r"\\'[0-9a-fA-F]{2}|\\[a-zA-Z]+-?\d* ?|[{}]", " ", content)
        elif extension == ".xml":
            content = "\n".join(_xml_text(data, include_values=True))
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    if not content:
        raise HTTPException(status_code=422, detail="文档中没有可抽取的文本；扫描版 PDF 需要先经过企业 OCR")
    max_chars = int(os.getenv("KNOWLEDGE_MAX_EXTRACTED_CHARS", "2000000"))
    return content[:max_chars], extension.lstrip(".")
