# -*- coding: utf-8 -*-
"""数据集管理：CSV/XLSX 解析、表头校验、time 校验、批量写入（T04）。

格式契约（01 §5）：
- 仅支持 CSV（UTF-8，首行表头，逗号分隔，支持引号转义）与 XLSX（第一个
  sheet，首行表头）。
- PRESSURE：必选 q，可选 a；SIMULATION：必选 q + time，可选 a。
- time 必须匹配 HH:mm:ss，换算 time_sec = HH*3600 + MM*60 + SS。
"""
from __future__ import annotations

import csv
import io
import re

from .. import errors
from ..config import settings
from ..persistence.db import Database
from . import dao

TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")

REQUIRED_HEADERS = {
    "PRESSURE": ("q",),
    "SIMULATION": ("q", "time"),
}
ALLOWED_HEADERS = ("q", "a", "time")


def parse_time(value: str) -> tuple[str, int]:
    """校验 HH:mm:ss，返回 (原文本, 当日秒数)；非法抛 ValidationError。"""
    m = TIME_RE.match((value or "").strip())
    if not m:
        raise errors.ValidationError(f"time 列格式必须为 HH:mm:ss: {value!r}")
    hh, mm, ss = (int(g) for g in m.groups())
    if hh > 23 or mm > 59 or ss > 59:
        raise errors.ValidationError(f"time 超出时刻范围: {value!r}")
    return m.group(0), hh * 3600 + mm * 60 + ss


def _norm_header(name: str) -> str:
    return (name or "").strip().lower()


def _rows_from_headers(
    headers: list[str], raw_rows: list[list], dataset_type: str,
) -> list[dict]:
    """按表头映射为用例行并校验（q 非空、time 合法）。"""
    cols = {_norm_header(h): i for i, h in enumerate(headers) if _norm_header(h)}
    for required in REQUIRED_HEADERS[dataset_type]:
        if required not in cols:
            raise errors.ValidationError(
                f"数据集缺少必选列 {required!r}（表头: {headers}）",
            )
    unknown = [h for h in cols if h not in ALLOWED_HEADERS]
    if unknown:
        raise errors.ValidationError(f"不认识的列: {unknown}（允许 q/a/time）")

    rows: list[dict] = []
    for line_no, raw in enumerate(raw_rows, start=2):
        def _col(key: str) -> str | None:
            i = cols.get(key)
            if i is None or i >= len(raw):
                return None
            v = raw[i]
            if v is None:
                return None
            if isinstance(v, float) and v.is_integer():
                # XLSX 数值单元格按整型文本处理（如序号被当作数值）
                return str(int(v))
            return str(v).strip()

        q = _col("q")
        if not q:
            raise errors.ValidationError(f"第 {line_no} 行 q 为空（q 列必填）")
        row: dict = {"q": q, "seq_no": len(rows) + 1}
        a = _col("a")
        if a:
            row["a"] = a
        if dataset_type == "SIMULATION":
            time_raw = _col("time") or ""
            send_time, time_sec = parse_time(time_raw)
            row["send_time"] = send_time
            row["time_sec"] = time_sec
        rows.append(row)
        if len(rows) > settings.perf_max_rows:
            raise errors.ValidationError(
                f"数据集超过最大行数 {settings.perf_max_rows}",
            )
    if not rows:
        raise errors.ValidationError("文件没有数据行（仅表头）")
    return rows


def parse_csv(data: bytes, dataset_type: str) -> list[dict]:
    """UTF-8 CSV（容忍 BOM），首行表头，标准引号转义。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise errors.ValidationError("CSV 必须为 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        raise errors.ValidationError("CSV 文件为空")
    return _rows_from_headers(headers, [r for r in reader], dataset_type)


def parse_xlsx(data: bytes, dataset_type: str) -> list[dict]:
    """XLSX：openpyxl 读第一个 sheet，首行表头；日期单元格直接拒绝。"""
    import openpyxl

    try:
        book = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise errors.ValidationError(
            f"XLSX 解析失败（需为有效 .xlsx 文件）: {type(exc).__name__}",
        ) from exc
    try:
        sheet = book.worksheets[0]
        raw_rows = []
        for row in sheet.iter_rows(values_only=True):
            if row is None:
                continue
            values = []
            for v in row:
                if hasattr(v, "strftime"):  # datetime/date/time 单元格
                    raise errors.ValidationError(
                        "XLSX 含日期/时间单元格，请转为文本（如 '09:30:00'）",
                    )
                values.append(v)
            raw_rows.append(values)
    finally:
        book.close()
    if not raw_rows:
        raise errors.ValidationError("XLSX 文件为空")
    return _rows_from_headers(raw_rows[0], raw_rows[1:], dataset_type)


def parse_upload(
    data: bytes, file_name: str, dataset_type: str,
) -> list[dict]:
    """按扩展名分派解析，统一执行大小/行数限制。"""
    if not data:
        raise errors.ValidationError("上传文件为空")
    if len(data) > settings.perf_max_upload_bytes:
        raise errors.PayloadTooLargeError(
            f"上传文件超过大小上限 {settings.perf_max_upload_bytes} 字节",
        )
    lower = (file_name or "").lower()
    if lower.endswith(".csv"):
        return parse_csv(data, dataset_type)
    if lower.endswith(".xlsx"):
        return parse_xlsx(data, dataset_type)
    raise errors.ValidationError("仅支持 CSV 与 XLSX 文件")


# ===========================================================================
# 服务层（创建/上传/删除/详情）
# ===========================================================================
def create_dataset(
    db: Database, *, master_id: str, name: str, dataset_type: str,
    created_by: str,
) -> dict:
    if dataset_type not in dao.DATASET_TYPES:
        raise errors.ValidationError(
            f"datasetType 必须为 {list(dao.DATASET_TYPES)}",
        )
    master = db.query_one(
        "SELECT master_id FROM eag_master_agent WHERE master_id=:mid",
        {"mid": master_id},
    )
    if not master:
        raise errors.NotFoundError("Agent Team 不存在")
    if dao.get_dataset_by_name(db, master_id, name):
        raise errors.ConflictError("该总控下已存在同名数据集")
    return dao.create_dataset(
        db, master_id=master_id, name=name, dataset_type=dataset_type,
        created_by=created_by,
    )


def upload_cases(
    db: Database, *, dataset_id: str, file_name: str, data: bytes,
) -> dict:
    """解析并写入用例；重复上传时先清空旧用例（幂等覆盖）。"""
    ds = dao.get_dataset(db, dataset_id)
    if not ds or ds["status"] != "ACTIVE":
        raise errors.NotFoundError("数据集不存在")
    rows = parse_upload(data, file_name, ds["dataset_type"])
    db.execute(
        "DELETE FROM eag_perf_case WHERE perf_dataset_id=:id",
        {"id": dataset_id},
    )
    db.execute(
        "UPDATE eag_perf_dataset SET case_count=0 WHERE perf_dataset_id=:id",
        {"id": dataset_id},
    )
    db.commit()
    n = dao.insert_cases(db, dataset_id, rows)
    return {
        "perf_dataset_id": dataset_id,
        "case_count": n,
        "replaced": bool(ds["case_count"]),
    }


def delete_dataset(db: Database, dataset_id: str) -> dict:
    """被测试记录引用时拒绝删除（01 §5.4：拒绝而非软删，历史可追溯）。"""
    ds = dao.get_dataset(db, dataset_id)
    if not ds or ds["status"] != "ACTIVE":
        raise errors.NotFoundError("数据集不存在")
    if dao.tests_referencing_dataset(db, dataset_id) > 0:
        raise errors.ConflictError(
            "数据集已被测试记录引用，不能删除（保留历史报告依据）",
        )
    dao.delete_dataset(db, dataset_id)
    return {"deleted": dataset_id}
