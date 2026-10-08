"""A2A 1.0 wire helpers; no legacy conversions."""
from google.protobuf.json_format import MessageToDict, ParseDict
from a2a.types import AgentCard, Part


def data_part(*, data, media_type="application/json"):
    return ParseDict({"data": data, "mediaType": media_type}, Part())


def card_dict(card):
    return MessageToDict(card)


def endpoint(card: dict) -> str:
    for interface in card.get("supportedInterfaces", []):
        if interface.get("protocolVersion") == "1.0" and interface.get("protocolBinding") == "JSONRPC":
            return interface["url"]
    raise ValueError("A2A 1.0 JSONRPC interface required")


def parse_card(card: dict):
    endpoint(card)
    return ParseDict(card, AgentCard())
