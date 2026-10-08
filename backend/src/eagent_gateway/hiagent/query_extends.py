"""The single HiAgent Inputs contract. Values never enter model prompts."""
from __future__ import annotations

import json
import math
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from google.protobuf.json_format import MessageToDict

INPUTS_EXTENSION = "https://eagent.example/extensions/hiagent-inputs/v1"
MASTER_EXTENSION = "https://eagent.example/extensions/hiagent-services/v1"
INPUTS_MEDIA = "application/vnd.eagent.hiagent-inputs+json"
MASTER_MEDIA = "application/vnd.eagent.hiagent-services+json"
INTERACTION_MEDIA = "application/vnd.eagent.hiagent-interaction+json"
RESPONSE_MEDIA = "application/vnd.eagent.hiagent-interaction-response+json"
CANVAS_CARD_MEDIA = "application/vnd.eagent.hiagent-canvas-card+json"
CARD_SUBMIT_EXTENSION = "https://eagent.example/extensions/master-card-submit/v1"
CARD_SUBMIT_MEDIA = "application/vnd.eagent.master-card-submit+json"
FLOW_EXTENSION = "https://eagent.example/extensions/master-flow/v1"
FLOW_MEDIA = "application/vnd.eagent.master-flow+json"
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_SECRET = re.compile(r"^(app_?key|api_?key|access_?token|refresh_?token|token|password|secret|authorization)$", re.I)
_RESERVED = {"capusername", "realname", "userid", "appkey", "appconversationid"}


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("File URL is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("File URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("File URL must not contain user information")
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _validate_relative_path(path: object) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("File metadata.hiagent.path must be a nonempty string")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    normalized_parts = path.replace("\\", "/").split("/")
    if posix.is_absolute() or windows.is_absolute() or windows.drive \
            or ".." in normalized_parts:
        raise ValueError("File metadata.hiagent.path must be relative and must not contain '..'")
    return path


def extract_files(message: dict, *, base_url: str | None) -> list[dict]:
    """Validate direct-HiAgent URL Parts and map them to QueryExtends.Files.

    ``MessageToDict`` represents protobuf Struct numbers as floats, so an
    integral float is accepted here and normalized back to an integer. Raw
    file Parts are deliberately unsupported: eAgent only forwards references.
    """
    files: list[dict] = []
    for part in message.get("parts", []):
        if "raw" in part:
            raise ValueError("Raw file Parts are not supported")
        if "url" not in part:
            continue
        if not base_url:
            raise ValueError("File Parts are only supported by direct HiAgent endpoints")
        url = part.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("File URL must be a nonempty string")
        if _origin(url) != _origin(base_url):
            raise ValueError("File URL must have the same origin as the HiAgent base URL")
        filename = part.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("File filename must be a nonempty string")
        media_type = part.get("mediaType")
        if not isinstance(media_type, str) or not media_type.strip():
            raise ValueError("File mediaType must be a nonempty string")
        metadata = part.get("metadata")
        hiagent = metadata.get("hiagent") if isinstance(metadata, dict) else None
        if not isinstance(hiagent, dict):
            raise ValueError("File metadata.hiagent is required")
        path = _validate_relative_path(hiagent.get("path"))
        size = hiagent.get("size")
        valid_size = isinstance(size, int) and not isinstance(size, bool) \
            and size >= 0
        if isinstance(size, float):
            valid_size = math.isfinite(size) and size >= 0 and int(size) == size
        if not valid_size:
            raise ValueError("File metadata.hiagent.size must be a nonnegative integer")
        files.append({
            "Name": filename,
            "Path": path,
            "Size": int(size),
            "Url": url,
        })
    return files


def _normalized_key(key: object) -> str:
    return re.sub(r"[_-]", "", str(key)).lower()


def _validate_input_key(key: object) -> None:
    if _SECRET.fullmatch(str(key)):
        raise ValueError("Credentials are forbidden in inputs")
    if _normalized_key(key) in _RESERVED:
        raise ValueError("System identity fields are forbidden in inputs")


def validate_input_keys(values: dict) -> None:
    """Reject credentials and adapter-owned identity fields at every depth."""
    def visit(value, depth=0):
        if depth > 8:
            raise ValueError("inputs nesting exceeds 8")
        if isinstance(value, dict):
            for key, child in value.items():
                _validate_input_key(key)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
    visit(values)


def validate_schema(schema: dict) -> None:
    if not isinstance(schema, dict) or len(json.dumps(schema).encode()) > 65536:
        raise ValueError("inputsSchema must be an object of at most 64 KiB")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Invalid inputsSchema") from exc
    if schema.get("type") != "object":
        raise ValueError("inputsSchema must describe an object")
    def visit(node, depth=0):
        if depth > 16:
            raise ValueError("Schema nesting exceeds 16")
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in node and not str(node[key]).startswith("#"):
                    raise ValueError("External schema references are forbidden")
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                raise ValueError("Objects must set additionalProperties=false")
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                for key in node["properties"]:
                    _validate_input_key(key)
            for value in node.values():
                visit(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                visit(value, depth + 1)
    visit(schema)


def _field_path(parts, field: str | None = None) -> str:
    path = ".".join(map(str, parts))
    if field:
        return f"{path}.{field}" if path else field
    return path or "inputs"


def _validation_error_message(error) -> str:
    """把 jsonschema 错误转为不包含实际业务值的字段级提示。"""
    if error.validator == "required" and isinstance(error.instance, dict):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [
            _field_path(error.absolute_path, str(name))
            for name in required if name not in error.instance
        ]
        if missing:
            return "缺少必填业务参数：" + "、".join(missing)
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected = " / ".join(map(str, expected))
        return f"业务参数 {_field_path(error.absolute_path)} 类型应为 {expected}（type）"
    if error.validator == "enum":
        allowed = json.dumps(
            error.validator_value, ensure_ascii=False, separators=(",", ":"),
        )[:512]
        return f"业务参数 {_field_path(error.absolute_path)} 允许值为 {allowed}（enum）"
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        declared = set((error.schema.get("properties") or {}).keys())
        patterns = list((error.schema.get("patternProperties") or {}).keys())
        extras = [
            str(key) for key in error.instance
            if key not in declared and not any(re.search(pattern, str(key)) for pattern in patterns)
        ]
        if extras:
            return "包含未声明的业务参数：" + "、".join(extras) + "（additionalProperties）"
    return (
        f"业务参数 {_field_path(error.absolute_path)} 未通过"
        f" {error.validator} 校验"
    )


def validate_values(values: dict, schema: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("inputs must be an object")
    if len(json.dumps(values, ensure_ascii=False, allow_nan=False).encode()) > 65536:
        raise ValueError("inputs exceeds 64 KiB")
    count = 0
    def visit(value, depth=0):
        nonlocal count
        if depth > 8:
            raise ValueError("inputs nesting exceeds 8")
        if isinstance(value, dict):
            count += len(value)
            if count > 128:
                raise ValueError("inputs exceeds 128 fields")
            for key, child in value.items():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
    validate_input_keys(values)
    visit(values)
    error = next(Draft202012Validator(schema).iter_errors(values), None)
    if error:
        # 不使用 validator 原始 message：其中可能包含实际输入值。
        raise ValueError(_validation_error_message(error)[:1024])
    return values


def extract_card_submission(message: dict) -> dict | None:
    parts = message.get("parts", [])
    candidates = [p for p in parts if p.get("mediaType") == CARD_SUBMIT_MEDIA]
    if not candidates:
        return None
    if len(candidates) != 1 or len(parts) != 2 or sum("text" in p for p in parts) != 1:
        raise ValueError("Card submission requires one text and one submission Data Part")
    data = candidates[0].get("data")
    if not isinstance(data, dict) or set(data) != {"cardId", "inputs"}:
        raise ValueError("Card submission requires cardId and full inputs")
    if not isinstance(data["cardId"], str) or not re.fullmatch(r"[a-f0-9]{32}", data["cardId"]):
        raise ValueError("Invalid cardId")
    if not isinstance(data["inputs"], dict):
        raise ValueError("Card inputs must be an object")
    return data


def extract_flow_release(message: dict) -> bool:
    """Master flow control: ``{"release": true}`` ends the context's active flow."""
    candidates = [p for p in message.get("parts", []) if p.get("mediaType") == FLOW_MEDIA]
    if not candidates:
        return False
    if len(candidates) != 1 or candidates[0].get("data") != {"release": True}:
        raise ValueError('Flow control requires exactly one {"release": true} Data Part')
    return True


def extract_parameters(message: dict, *, master=False) -> dict:
    media = MASTER_MEDIA if master else INPUTS_MEDIA
    candidates = [p for p in message.get("parts", []) if p.get("mediaType") == media]
    if master and not candidates:
        return {}
    if len(candidates) != 1:
        raise ValueError("Exactly one parameter Data Part is required")
    data = candidates[0].get("data")
    key = "services" if master else "inputs"
    if not isinstance(data, dict) or set(data) != {key} or not isinstance(data[key], dict):
        raise ValueError(f"Expected {key} parameter envelope")
    return data[key]


def parameters_from_context(context, *, master=False) -> dict:
    return extract_parameters(MessageToDict(context.message), master=master)


def files_from_context(context, *, base_url: str | None) -> list[dict]:
    return extract_files(MessageToDict(context.message), base_url=base_url)


def args_hint(services: dict) -> str:
    return "业务参数已由系统按服务传递；不得生成、改写或向用户追问参数值。缺参时说明配置缺失。"
