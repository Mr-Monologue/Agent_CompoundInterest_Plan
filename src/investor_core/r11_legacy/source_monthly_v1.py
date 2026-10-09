"""Bounded, versioned extraction of the observed NBS/PBC publication layouts.

This is source preparation, not a new economic method or a promotion receipt.
HTML scripts, spreadsheet formulas and external resources are never executed.
"""

# ruff: noqa: RUF001 -- exact Chinese publisher labels and punctuation

from __future__ import annotations

import io
import re
import zipfile
from calendar import monthrange
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from investor_core.r11_source_archive import OriginalEnvelope
from investor_core.r11_source_json import LIMITATIONS

HTML_VERSION = "r11-nbs-pbc-html-v1"
XLSX_VERSION = "r11-pbc-stock-xlsx-v1"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 8 * 1024 * 1024


def compact(text: str) -> str:
    return "".join(text.split())


def month_end(year: int, month: int) -> str:
    if not 1900 <= year <= 2100 or not 1 <= month <= 12:
        raise ValueError("invalid data period")
    return f"{year:04}-{month:02}-{monthrange(year, month)[1]:02}"


class PublicationHTML(HTMLParser):
    def __init__(self, text: str, *, collect_tables: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.paragraphs: list[dict[str, Any]] = []
        self.title: list[str] = []
        self.visible: list[str] = []
        self.publication_dates: list[str] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.paragraph: list[str] | None = None
        self.paragraph_start = (0, 0)
        self.in_title = False
        self.ignored = False
        self.nodes = 0
        self.collect_tables = collect_tables
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes += 1
        if self.nodes > 100_000:
            raise ValueError("HTML node limit exceeded")
        if tag in {"script", "style"}:
            self.ignored = True
        if self.ignored:
            return
        if tag == "meta":
            attributes = dict(attrs)
            if attributes.get("name") == "PubDate" and attributes.get("content"):
                self.publication_dates.append(str(attributes["content"]))
        if tag == "title":
            self.in_title = True
        elif tag == "table" and self.collect_tables:
            if self.table is not None:
                raise ValueError("nested tables unsupported")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "p":
            self.paragraph = []
            self.paragraph_start = self.getpos()

    def handle_data(self, data: str) -> None:
        if self.ignored:
            return
        self.visible.append(data)
        if self.in_title:
            self.title.append(data)
        if self.cell is not None:
            self.cell.append(data)
        if self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored = False
        if self.ignored:
            return
        if tag == "title":
            self.in_title = False
        elif tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(compact("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None
        elif tag == "p" and self.paragraph is not None:
            self.paragraphs.append(
                dict(
                    text=compact("".join(self.paragraph)),
                    start_line=self.paragraph_start[0],
                    start_column=self.paragraph_start[1],
                )
            )
            self.paragraph = None


def monthly_html(original: OriginalEnvelope) -> dict[str, Any]:
    if original.media_type != "text/html" or original.charset != "utf-8":
        raise ValueError("specified UTF-8 publication required")
    host = urlsplit(original.request_url).hostname
    if host not in {"www.stats.gov.cn", "www.pbc.gov.cn"}:
        raise ValueError("unsupported publication layout")
    if urlsplit(original.final_url).hostname != host:
        raise ValueError("cross-host redirect unsupported")
    parsed = PublicationHTML(
        original.raw_bytes().decode("utf-8", errors="strict"),
        collect_tables=host == "www.stats.gov.cn",
    )
    title = compact("".join(parsed.title))
    period = re.search(r"(20\d{2})年(\d{1,2})月", title)
    half_year = re.fullmatch(r"(20\d{2})年上半年金融统计数据报告", title)
    if period is None and host == "www.pbc.gov.cn" and half_year is not None:
        year, month = int(half_year[1]), 6
    elif period is not None:
        year, month = map(int, period.groups())
    else:
        raise ValueError("unambiguous publication period required")
    if host == "www.stats.gov.cn":
        if "中国采购经理指数运行情况" not in title:
            raise ValueError("not a PMI release")
        matches: list[dict[str, Any]] = []
        for table_index, table in enumerate(parsed.tables):
            if not any("PMI" in row for row in table[:4]):
                continue
            for row_index, row in enumerate(table):
                if len(row) == 7 and row[0] == f"{year}年{month}月":
                    if not re.fullmatch(r"\d{1,3}\.\d+", row[1]):
                        raise ValueError("invalid PMI cell")
                    matches.append(
                        dict(
                            value=row[1],
                            locator=dict(
                                table_index=table_index,
                                row_index=row_index,
                                column_index=1,
                                period_cell=row[0],
                                header="PMI",
                                raw_text=row[1],
                            ),
                        )
                    )
        identity, unit = "NBS_MANUFACTURING_PMI", "PMI_POINTS"
        if len(matches) > 1:
            first_table = parsed.tables[matches[0]["locator"]["table_index"]]
            if all(parsed.tables[m["locator"]["table_index"]] == first_table for m in matches):
                # Desktop/mobile copies occur in the observed pages. Keep every
                # locator and require the entire table to agree, not just the value.
                matches[0]["duplicate_locations"] = [m["locator"] for m in matches[1:]]
                matches = matches[:1]
    else:
        if "金融统计数据报告" not in title:
            raise ValueError("not a financial statistics release")
        pattern = re.compile(
            rf"初步统计，{year}年{month}月末社会融资规模存量为"
            r"(?P<stock>\d+(?:\.\d+)?)万亿元，同比增长(?P<yoy>\d+(?:\.\d+)?)%。"
        )
        matches = []
        for paragraph in parsed.paragraphs:
            match = pattern.match(paragraph["text"])
            if match:
                matches.append(
                    dict(
                        value=match["yoy"],
                        stock_trillion_cny=match["stock"],
                        locator={**paragraph, "raw_text": match[0]},
                    )
                )
        identity, unit = "PBC_TSF_STOCK_YOY_SAME_BASIS", "PERCENT"
    if len(matches) != 1:
        raise ValueError("missing or ambiguous monthly extraction")
    publication_dates = list(parsed.publication_dates)
    if host == "www.stats.gov.cn":
        publication_dates.extend(
            re.findall(
                r"发布时间：\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)", "".join(parsed.visible)
            )
        )
    texts = sorted({" ".join(text.split()) for text in publication_dates})
    dates = []
    for text in texts:
        if not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", text):
            raise ValueError("unrecognized publication date syntax")
        dates.append(date.fromisoformat(text[:10].replace("/", "-")).isoformat())
    dates = sorted(set(dates))
    if len(dates) > 1:
        raise ValueError("conflicting publication dates")
    publication = dict(date=None, precision="UNKNOWN", timezone=None, absolute_time=None)
    if dates:
        publication = dict(
            date=date.fromisoformat(dates[0]).isoformat(),
            precision="DATE",
            timezone=None,
            absolute_time=None,
        )
    return dict(
        extractor_version=HTML_VERSION,
        original_sha256=original.sha256,
        publication_title=title,
        publication=publication,
        publication_source_texts=texts,
        normalization="HTML_ENTITY_DECODE_AND_WHITESPACE_REMOVAL; NO_NUMERIC_ROUNDING",
        identity=identity,
        unit=unit,
        day=month_end(year, month),
        **matches[0],
        publication_timezone_verified=False,
        vintage=original.sha256,
        limitations=[*LIMITATIONS, "PUBLICATION_TIMEZONE_AND_VINTAGE_NOT_AUTHENTICATED"],
    )


def _xml(raw: bytes) -> ET.Element:
    if len(raw) > MAX_XML_BYTES or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML declarations or resource limit unsupported")
    # Only UTF-8 is accepted, so alternate encodings cannot hide declarations.
    raw.decode("utf-8", errors="strict")
    if b"\x00" in raw:
        raise ValueError("non UTF-8 XML unsupported")
    count = depth = 0
    iterator = ET.iterparse(io.BytesIO(raw), events=("start", "end"))
    root = None
    for event, element in iterator:
        if event == "start":
            if root is None:
                root = element
            count += 1
            depth += 1
            if count > 100_000 or depth > 64:
                raise ValueError("XML node/depth limit exceeded")
        else:
            depth -= 1
    if root is None:
        raise ValueError("empty XML")
    return root


def pbc_stock_xlsx(original: OriginalEnvelope) -> dict[str, Any]:
    if original.media_type != "application/xlsx":
        raise ValueError("specified XLSX publication required")
    if any(
        urlsplit(url).hostname != "www.pbc.gov.cn"
        for url in (original.request_url, original.final_url)
    ):
        raise ValueError("unsupported workbook source")
    with zipfile.ZipFile(io.BytesIO(original.raw_bytes())) as archive:
        members = archive.infolist()
        if len(members) > 128 or len({m.filename for m in members}) != len(members):
            raise ValueError("ZIP member limit or duplicate name")
        if sum(m.file_size for m in members) > MAX_ZIP_EXPANDED_BYTES:
            raise ValueError("ZIP expanded resource limit exceeded")
        for member in members:
            if (
                member.file_size > MAX_XML_BYTES
                or member.flag_bits & 1
                or member.file_size > 200 * max(1, member.compress_size)
                or ".." in member.filename.split("/")
                or "\\" in member.filename
                or member.filename.startswith("/")
                or "externalLinks" in member.filename
                or member.filename.endswith(".bin")
            ):
                raise ValueError("unsafe or oversized ZIP member")

        def read_xml(name: str) -> ET.Element:
            with archive.open(name) as stream:
                raw = stream.read(MAX_XML_BYTES + 1)
            return _xml(raw)

        workbook = read_xml("xl/workbook.xml")
        sheets = workbook.findall(f"{NS}sheets/{NS}sheet")
        if len(sheets) != 1:
            raise ValueError("single observed worksheet required")
        relationships = read_xml("xl/_rels/workbook.xml.rels")
        sheet_id = sheets[0].get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        links = [r for r in relationships if r.get("Id") == sheet_id]
        if len(links) != 1 or links[0].get("Target") != "worksheets/sheet1.xml":
            raise ValueError("unexpected worksheet relationship")
        if any(r.get("TargetMode") == "External" for r in relationships):
            raise ValueError("external relationships unsupported")
        strings = [
            "".join(t.text or "" for t in item.iter(f"{NS}t"))
            for item in read_xml("xl/sharedStrings.xml")
        ]
        sheet = read_xml("xl/worksheets/sheet1.xml")
        cells: dict[str, str] = {}
        for cell in sheet.iter(f"{NS}c"):
            ref = cell.get("r", "")
            if ref in cells or cell.find(f"{NS}f") is not None:
                raise ValueError("duplicate cell or formula unsupported")
            value = cell.findtext(f"{NS}v", "")
            if cell.get("t") == "s":
                if not value.isdigit() or not 0 <= int(value) < len(strings):
                    raise ValueError("invalid shared string")
                value = strings[int(value)]
            elif cell.get("t") not in {None, "n"}:
                raise ValueError("unsupported cell type")
            cells[ref] = compact(value)
        if (
            cells.get("A1") != "社会融资规模存量统计表"
            or cells.get("A8") != "社会融资规模存量"
            or cells.get("A3") != "单位：万亿元人民币"
        ):
            raise ValueError("unrecognized stock table layout")
        points = []
        empty_periods = []
        for index in range(12):
            stock_col, growth_col = chr(ord("B") + index * 2), chr(ord("C") + index * 2)
            period = cells.get(stock_col + "5", "")
            match = re.fullmatch(r"(20\d{2})\.(\d{1,2})", period)
            if (
                match is None
                or int(match[2]) != index + 1
                or cells.get(stock_col + "6") != "存量"
                or cells.get(growth_col + "6") != "增速（%）"
            ):
                raise ValueError("unrecognized period/unit headers")
            day = month_end(int(match[1]), int(match[2]))
            value = cells.get(growth_col + "8", "")
            stock = cells.get(stock_col + "8", "")
            if not value and not stock:
                empty_periods.append(day)
                continue
            if not all(re.fullmatch(r"\d+(?:\.\d+)?", v) for v in (value, stock)):
                raise ValueError("missing or invalid monthly pair")
            points.append(
                dict(
                    day=day,
                    value=value,
                    stock_trillion_cny=stock,
                    locator=dict(
                        sheet_name=sheets[0].get("name"),
                        sheet_part="xl/worksheets/sheet1.xml",
                        cell=growth_col + "8",
                        stock_cell=stock_col + "8",
                        period_cell=stock_col + "5",
                        unit_cell=growth_col + "6",
                        raw_text=value,
                    ),
                )
            )
    return dict(
        extractor_version=XLSX_VERSION,
        original_sha256=original.sha256,
        identity="PBC_TSF_STOCK_YOY_SAME_BASIS",
        unit="PERCENT",
        points=points,
        empty_periods=empty_periods,
        vintage=original.sha256,
        limitations=[*LIMITATIONS, "LATER_YEAR_TABLE_IS_NOT_EACH_MONTHS_ORIGINAL_VINTAGE"],
    )
