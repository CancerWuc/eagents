"""Performance data response projections shared by list views."""


def _str_ts(v) -> str | None:
    return str(v) if v else None


def _dataset_out(ds: dict) -> dict:
    return {
        "perf_dataset_id": ds["perf_dataset_id"],
        "master_id": ds["master_id"],
        "name": ds["name"],
        "dataset_type": ds["dataset_type"],
        "status": ds["status"],
        "case_count": ds["case_count"],
        "created_by": ds["created_by"],
        "created_at": _str_ts(ds["created_at"]),
        "updated_at": _str_ts(ds["updated_at"]),
    }
