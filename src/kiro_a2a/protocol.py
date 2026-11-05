"""A2A wire versions.

Inside the bridge everything uses the A2A v0.3 JSON shapes (`kind` fields,
lower-case states). This module converts to and from A2A 1.0, which differs
in method names, enum spellings, part fields and result wrappers:

  0.3                               1.0
  message/send                      SendMessage        result {"task": ...} / {"message": ...}
  message/stream                    SendStreamingMessage
  tasks/get, tasks/cancel           GetTask, CancelTask
  tasks/resubscribe                 SubscribeToTask
  tasks/pushNotificationConfig/*    Create/Get/List/DeleteTaskPushNotificationConfig
  "completed", "input-required"     "TASK_STATE_COMPLETED", "TASK_STATE_INPUT_REQUIRED"
  "user" / "agent"                  "ROLE_USER" / "ROLE_AGENT"
  {"kind": "text", "text": ...}     {"text": ...}
  {"kind": "file", "file": {...}}   {"raw": ...} or {"url": ...}, plus mediaType, filename
  stream event {"kind": ...}        {"statusUpdate": ...} / {"artifactUpdate": ...} / {"task": ...}
"""

from __future__ import annotations

V1_METHODS = {
    "SendMessage": "message/send",
    "SendStreamingMessage": "message/stream",
    "GetTask": "tasks/get",
    "CancelTask": "tasks/cancel",
    "ListTasks": "tasks/list",
    "SubscribeToTask": "tasks/resubscribe",
    "CreateTaskPushNotificationConfig": "tasks/pushNotificationConfig/set",
    "GetTaskPushNotificationConfig": "tasks/pushNotificationConfig/get",
    "ListTaskPushNotificationConfigs": "tasks/pushNotificationConfig/list",
    "DeleteTaskPushNotificationConfig": "tasks/pushNotificationConfig/delete",
}
V03_TO_V1_METHODS = {v: k for k, v in V1_METHODS.items()}

_STATES = ["submitted", "working", "input-required", "completed", "canceled", "failed", "rejected", "auth-required"]
STATE_TO_V1 = {s: "TASK_STATE_" + s.upper().replace("-", "_") for s in _STATES} | {"unknown": "TASK_STATE_UNSPECIFIED"}
STATE_FROM_V1 = {v: k for k, v in STATE_TO_V1.items()} | {"TASK_STATE_CANCELLED": "canceled"}
ROLE_TO_V1 = {"user": "ROLE_USER", "agent": "ROLE_AGENT"}
ROLE_FROM_V1 = {v: k for k, v in ROLE_TO_V1.items()}
FINAL_STATES = {"completed", "canceled", "failed", "rejected", "input-required", "auth-required"}


# ---- parts and messages -------------------------------------------------------

def part_to_v1(part: dict) -> dict:
    out = {k: v for k, v in part.items() if k in ("metadata",)}
    kind = part.get("kind")
    if kind == "text" or (kind is None and "text" in part):
        out["text"] = part.get("text", "")
    elif kind == "data" or (kind is None and "data" in part):
        out["data"] = part.get("data")
    elif kind == "file":
        file = part.get("file") or {}
        if "bytes" in file:
            out["raw"] = file["bytes"]
        else:
            out["url"] = file.get("uri")
        if file.get("mimeType"):
            out["mediaType"] = file["mimeType"]
        if file.get("name"):
            out["filename"] = file["name"]
    return out


def part_from_v1(part: dict) -> dict:
    if "kind" in part:
        return part
    out = {k: v for k, v in part.items() if k in ("metadata",)}
    if "text" in part:
        return {**out, "kind": "text", "text": part["text"]}
    if "data" in part:
        return {**out, "kind": "data", "data": part["data"]}
    file = {}
    if "raw" in part:
        file["bytes"] = part["raw"]
    elif "url" in part:
        file["uri"] = part["url"]
    if part.get("mediaType"):
        file["mimeType"] = part["mediaType"]
    if part.get("filename"):
        file["name"] = part["filename"]
    return {**out, "kind": "file", "file": file}


def message_to_v1(message: dict) -> dict:
    out = {k: v for k, v in message.items() if k not in ("kind", "parts", "role")}
    out["role"] = ROLE_TO_V1.get(message.get("role"), message.get("role"))
    out["parts"] = [part_to_v1(p) for p in message.get("parts") or []]
    return out


def message_from_v1(message: dict) -> dict:
    out = {k: v for k, v in message.items() if k not in ("parts", "role")}
    out["kind"] = "message"
    out["role"] = ROLE_FROM_V1.get(message.get("role"), message.get("role") or "user")
    out["parts"] = [part_from_v1(p) for p in message.get("parts") or []]
    return out


# ---- tasks and events -----------------------------------------------------------

def _status_to_v1(status: dict) -> dict:
    out = {k: v for k, v in status.items() if k not in ("state", "message")}
    out["state"] = STATE_TO_V1.get(status.get("state"), "TASK_STATE_UNSPECIFIED")
    if status.get("message"):
        out["message"] = message_to_v1(status["message"])
    return out


def _status_from_v1(status: dict) -> dict:
    out = {k: v for k, v in status.items() if k not in ("state", "message")}
    out["state"] = STATE_FROM_V1.get(status.get("state"), status.get("state") or "unknown")
    if status.get("message"):
        out["message"] = message_from_v1(status["message"])
    return out


def _artifact_to_v1(artifact: dict) -> dict:
    return {**artifact, "parts": [part_to_v1(p) for p in artifact.get("parts") or []]}


def _artifact_from_v1(artifact: dict) -> dict:
    return {**artifact, "parts": [part_from_v1(p) for p in artifact.get("parts") or []]}


def task_to_v1(task: dict) -> dict:
    out = {k: v for k, v in task.items() if k not in ("kind", "status", "artifacts", "history")}
    out["status"] = _status_to_v1(task.get("status") or {})
    if task.get("artifacts"):
        out["artifacts"] = [_artifact_to_v1(a) for a in task["artifacts"]]
    if task.get("history"):
        out["history"] = [message_to_v1(m) for m in task["history"]]
    return out


def task_from_v1(task: dict) -> dict:
    out = {k: v for k, v in task.items() if k not in ("status", "artifacts", "history")}
    out["kind"] = "task"
    out["status"] = _status_from_v1(task.get("status") or {})
    out["artifacts"] = [_artifact_from_v1(a) for a in task.get("artifacts") or []]
    out["history"] = [message_from_v1(m) for m in task.get("history") or []]
    return out


def result_to_v1(result: dict) -> dict:
    """A Task or Message (send result / stream event / push payload) in 1.0 form."""
    kind = result.get("kind")
    if kind == "task":
        return {"task": task_to_v1(result)}
    if kind == "message":
        return {"message": message_to_v1(result)}
    if kind == "status-update":
        body = {k: v for k, v in result.items() if k not in ("kind", "final", "status")}
        return {"statusUpdate": {**body, "status": _status_to_v1(result.get("status") or {})}}
    if kind == "artifact-update":
        body = {k: v for k, v in result.items() if k not in ("kind", "artifact")}
        return {"artifactUpdate": {**body, "artifact": _artifact_to_v1(result.get("artifact") or {})}}
    return result


def result_from_v1(result: dict) -> dict:
    if "kind" in result:
        return result
    if "task" in result:
        return task_from_v1(result["task"])
    if "message" in result:
        return message_from_v1(result["message"])
    if "statusUpdate" in result:
        body = dict(result["statusUpdate"])
        status = _status_from_v1(body.pop("status", {}) or {})
        return {**body, "kind": "status-update", "status": status, "final": status["state"] in FINAL_STATES}
    if "artifactUpdate" in result:
        body = dict(result["artifactUpdate"])
        return {**body, "kind": "artifact-update", "artifact": _artifact_from_v1(body.get("artifact") or {})}
    if "id" in result and "status" in result:  # a bare Task (GetTask, CancelTask)
        return task_from_v1(result)
    return result


# ---- agent cards ----------------------------------------------------------------

_SCHEME_WRAPPERS = {
    "apiKey": "apiKeySecurityScheme",
    "http": "httpAuthSecurityScheme",
    "oauth2": "oauth2SecurityScheme",
    "openIdConnect": "openIdConnectSecurityScheme",
    "mutualTLS": "mtlsSecurityScheme",
}
_SCHEME_TYPES = {v: k for k, v in _SCHEME_WRAPPERS.items()}


def scheme_from_any(scheme: dict) -> dict:
    """A security scheme in 0.3/OpenAPI form, whichever version the card used."""
    if "type" in scheme:
        return scheme
    for wrapper, kind in _SCHEME_TYPES.items():
        if wrapper in scheme:
            return {"type": kind, **(scheme[wrapper] or {})}
    return scheme


def requirements_from_any(card: dict) -> list[dict]:
    """[{scheme name: [scopes]}] from `security` (0.3) or `securityRequirements` (1.0)."""
    if card.get("security"):
        return card["security"]
    out = []
    for requirement in card.get("securityRequirements") or []:
        schemes = requirement.get("schemes", requirement)
        out.append({
            name: (value.get("list", []) if isinstance(value, dict) else list(value or []))
            for name, value in schemes.items()
        })
    return out


def card_to_v1(card: dict, url: str) -> dict:
    """The 1.0 form of a card built in 0.3 form."""
    drop = ("protocolVersion", "url", "preferredTransport", "additionalInterfaces", "security",
            "securitySchemes", "supportedInterfaces", "signatures")
    out = {k: v for k, v in card.items() if k not in drop}
    out["supportedInterfaces"] = [{"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]
    if card.get("securitySchemes"):
        out["securitySchemes"] = {
            name: {_SCHEME_WRAPPERS[s["type"]]: {k: v for k, v in s.items() if k != "type"}}
            for name, s in card["securitySchemes"].items()
        }
        out["securityRequirements"] = [
            {"schemes": {name: {"list": scopes} for name, scopes in req.items()}} for req in card.get("security") or []
        ]
    return out


def pick_interface(card: dict) -> tuple[str | None, str]:
    """(JSON-RPC URL, wire version) a client should use for this card."""
    if card.get("url") and str(card.get("protocolVersion", "0.3")).startswith("0."):
        return card["url"], "0.3"
    interfaces = [i for i in card.get("supportedInterfaces") or [] if i.get("protocolBinding", "JSONRPC") == "JSONRPC"]
    for wanted in ("0.3", "1.0"):
        for interface in interfaces:
            if str(interface.get("protocolVersion", "")).startswith(wanted):
                return interface.get("url"), "0.3" if wanted == "0.3" else "1.0"
    if interfaces:
        return interfaces[0].get("url"), "1.0"
    return card.get("url"), "0.3"
