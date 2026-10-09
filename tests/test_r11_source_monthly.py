"""Publication-specific positive cases and ambiguity/resource negative cases."""

# ruff: noqa: RUF001 -- exact source labels

import copy
import io
import zipfile

import pytest
from test_r11_source_archive import original, request

from investor_core.execution import ExecutionService
from investor_core.r11_source_archive import archive_original
from investor_core.r11_source_monthly import monthly_html, pbc_stock_xlsx
from investor_core.r11_source_preparation import prepare_sources, replay_preparation


def html(text, host="www.stats.gov.cn"):
    return original(
        text.encode(),
        media_type="text/html",
        request_url=f"https://{host}/test",
        final_url=f"https://{host}/test",
    )


PMI = """<title>2026年9月中国采购经理指数运行情况</title>
<script>evil();</script><table><tr><td></td><td>PMI</td><td></td></tr>
<tr><td>2026年9月</td><td>50.1</td><td>51.7</td><td>50.5</td><td>48.2</td>
<td>48.4</td><td>50.1</td></tr></table>"""
PBC = """<title>2026年8月金融统计数据报告</title>
<p>初步统计，2026年8月末社会融资规模存量为464.8万亿元，同比增长7.2%。其中其他内容。</p>"""


def test_pmi_specific_column_and_publication():
    value = monthly_html(html(PMI))
    assert (value["day"], value["value"], value["unit"]) == ("2026-09-30", "50.1", "PMI_POINTS")
    assert value["locator"]["column_index"] == 1
    assert not value["publication_timezone_verified"]
    dated = monthly_html(html(PMI + "<font>发布时间：2026-09-30&nbsp;09:30</font>"))
    assert dated["publication"] == dict(
        date="2026-09-30", precision="DATE", timezone=None, absolute_time=None
    )
    with pytest.raises(ValueError, match="ambiguous"):
        monthly_html(html(PMI + PMI[PMI.index("<table>") :].replace("50.1", "50.2")))
    duplicate = monthly_html(html(PMI + PMI[PMI.index("<table>") :]))
    assert len(duplicate["duplicate_locations"]) == 1
    with pytest.raises(ValueError, match="ambiguous"):
        monthly_html(html(PMI.replace("PMI", "商务活动")))


def test_pbc_stock_not_increment_and_location():
    value = monthly_html(html(PBC, "www.pbc.gov.cn"))
    assert value["value"] == "7.2" and value["stock_trillion_cny"] == "464.8"
    assert value["locator"]["start_line"] == 2
    half = PBC.replace("2026年8月金融", "2026年上半年金融").replace("2026年8月末", "2026年6月末")
    assert monthly_html(html(half, "www.pbc.gov.cn"))["day"] == "2026-06-30"
    with pytest.raises(ValueError):
        monthly_html(html(PBC.replace("存量", "增量"), "www.pbc.gov.cn"))
    with pytest.raises(ValueError):
        monthly_html(html(PBC.replace("2026年8月末", "2026年7月末"), "www.pbc.gov.cn"))


@pytest.mark.parametrize("conflicting", [False, True])
def test_pmi_repeated_period_in_one_table_rejected(conflicting):
    row = PMI[PMI.index("<tr><td>2026") : PMI.index("</table>")]
    if conflicting:
        row = row.replace("50.1", "99.9")
    with pytest.raises(ValueError, match="ambiguous repeated period"):
        monthly_html(html(PMI.replace("</table>", row + "</table>")))


@pytest.mark.parametrize("header", ["<td>other</td><td>PMI</td>", "<td>PMI</td><td>PMI</td>"])
def test_pmi_shifted_or_duplicate_header_rejected(header):
    with pytest.raises(ValueError, match="header layout"):
        monthly_html(html(PMI.replace("<td>PMI</td><td></td>", header)))


def workbook(*, formula=False, bomb=False, duplicate=False):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    strings = [
        "社会融资规模存量统计表",
        "社会融资规模存量",
        "单位：万亿元人民币",
        "存量",
        "增速（%）",
    ]
    cells = [
        '<c r="A1" t="s"><v>0</v></c>',
        '<c r="A8" t="s"><v>1</v></c>',
        '<c r="A3" t="s"><v>2</v></c>',
    ]
    for month in range(1, 13):
        stock, growth = chr(64 + month * 2), chr(65 + month * 2)
        cells.extend(
            [
                f'<c r="{stock}5"><v>2026.{month}</v></c>',
                f'<c r="{stock}6" t="s"><v>3</v></c>',
                f'<c r="{growth}6" t="s"><v>4</v></c>',
            ]
        )
        if month <= 8:
            cells.extend(
                [
                    f'<c r="{stock}8"><v>464.8</v></c>',
                    f'<c r="{growth}8">' + ("<f>1+1</f>" if formula else "") + "<v>7.2</v></c>",
                ]
            )
    files = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{ns}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships><Relationship Id="rId1" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/sharedStrings.xml": f'<sst xmlns="{ns}">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}">' + "".join(cells) + "</worksheet>",
    }
    if bomb:
        files["padding.xml"] = "x" * 1000000
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        if duplicate:
            archive.writestr("xl/sharedStrings.xml", files["xl/sharedStrings.xml"])
    return original(
        stream.getvalue(),
        media_type="application/xlsx",
        charset=None,
        request_url="https://www.pbc.gov.cn/test.xlsx",
        final_url="https://www.pbc.gov.cn/test.xlsx",
    )


def test_xlsx_blank_future_months_not_filled():
    result = pbc_stock_xlsx(workbook())
    assert len(result["points"]) == 8
    assert result["points"][-1]["locator"]["cell"] == "Q8"
    assert result["empty_periods"] == ["2026-09-30", "2026-10-31", "2026-11-30", "2026-12-31"]


@pytest.mark.parametrize("value,extra", [("7.2", "7.2"), ("7.2", "99.9"), ("0", "0"), ("0", "1")])
def test_xlsx_duplicate_value_nodes_rejected(value, extra):
    source = workbook()
    stream = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(source.raw_bytes())) as before,
        zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as after,
    ):
        for name in before.namelist():
            raw = before.read(name)
            if name == "xl/worksheets/sheet1.xml":
                raw = raw.replace(
                    f"<v>{value}</v>".encode(), f"<v>{value}</v><v>{extra}</v>".encode(), 1
                )
            after.writestr(name, raw)
    modified = original(
        stream.getvalue(),
        media_type="application/xlsx",
        charset=None,
        request_url=source.request_url,
        final_url=source.final_url,
    )
    with pytest.raises(ValueError, match="duplicate cell value"):
        pbc_stock_xlsx(modified)


@pytest.mark.parametrize("kind", ["formula", "bomb", "duplicate"])
def test_xlsx_resource_and_execution_boundaries(kind):
    with pytest.raises(ValueError):
        pbc_stock_xlsx(workbook(**{kind: True}))


def test_preparation_replays_without_shadow_or_business_writes(tmp_path):
    from test_daily_client import dump
    from test_planning import configured_services

    from investor_core.research import ResearchService

    db = tmp_path / "isolated.db"
    _, planning, _, _ = configured_services(db)
    research = ResearchService(planning.settings)
    saved = archive_original(ExecutionService(research), request(html(PMI)))
    before = dump(db)
    report = prepare_sources(research, "MONTHLY", [saved["id"]])
    assert replay_preparation(report)["result"] == "REPRODUCED"
    assert not report["eligible_forward_observation"] and not report["promotion_eligible"]
    assert dump(db) == before
    from investor_core.r11_source_preparation import LEGACY_VERSION, _extract
    from investor_core.scheduler import digest

    historical = copy.deepcopy(report)
    historical["preparation_version"] = LEGACY_VERSION
    historical["output"] = _extract("MONTHLY", historical["sources"], historical=True)
    historical["report_hash"] = digest({k: v for k, v in historical.items() if k != "report_hash"})
    replay = replay_preparation(historical)
    assert replay["result"] == "REPRODUCED" and replay["historical_parser"]
    assert not replay["qualifies_current_preparation"]
    changed = copy.deepcopy(report)
    changed["output"]["extractions"][0]["value"] = "99.9"
    assert replay_preparation(changed)["result"] == "MISMATCH"
    changed = copy.deepcopy(report)
    changed["preparation_version"] = "future"
    with pytest.raises(ValueError):
        replay_preparation(changed)


def test_known_monthly_arithmetic_does_not_qualify_observation():
    from investor_core.r11_source_preparation import _monthly_arithmetic

    points = []
    for month, value in [(6, "50.3"), (7, "49.2"), (8, "49.8"), (9, "50.1")]:
        text = PMI.replace("2026年9月", f"2026年{month}月").replace("50.1", value)
        points.append(monthly_html(html(text)))
    for month, value in [(5, "7.7"), (6, "7.4"), (7, "7.4"), (8, "7.2")]:
        text = PBC.replace("2026年8月", f"2026年{month}月").replace("7.2", value)
        points.append(monthly_html(html(text, "www.pbc.gov.cn")))
    result = _monthly_arithmetic(points)
    assert result["values"] == {"F": "-0.025", "L": "-0.25"}
    assert result["formal_dimension_scores"] is None
    assert result["effective_forward_weeks"] == 0 and result["season"] == "UNKNOWN"
    assert _monthly_arithmetic(points + points[:1])["values"]["F"] is None
