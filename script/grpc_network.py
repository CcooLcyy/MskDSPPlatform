"""仅通过固定 Unix socket 管理宿主机 gRPC 端口分段规则。"""
import http.client
import json
import socket


class ControlError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def strict_json(raw):
    def fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 不接受重复字段")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=fields)


def validate_settings(value):
    if (not isinstance(value, dict) or set(value) != {"enabled", "mss", "revision"}
            or type(value["enabled"]) is not bool or type(value["mss"]) is not int
            or not 536 <= value["mss"] <= 1460
            or type(value["revision"]) is not int or value["revision"] < 0):
        raise ValueError("设置必须且只能包含 enabled、536 至 1460 的整数 mss 和非负整数 revision")
    return value


def validate_status(value):
    if (not isinstance(value, dict)
            or not {"enabled", "mss", "revision", "port", "persistent", "applied", "matched_packets"} <= set(value)
            or type(value["enabled"]) is not bool or type(value["mss"]) is not int
            or not 536 <= value["mss"] <= 1460
            or type(value["revision"]) is not int or value["revision"] < 0
            or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
            or type(value["persistent"]) is not bool or type(value["applied"]) is not bool
            or type(value["matched_packets"]) is not int or value["matched_packets"] < 0
            or ("error" in value and not isinstance(value["error"], str))):
        raise ControlError("宿主机网络管理服务返回了无效状态")
    return value


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.path = str(path)

    def connect(self):
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("当前系统不支持 Unix socket")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(socket_path, settings=None):
    if settings is not None:
        validate_settings(settings)
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request("POST" if settings is not None else "GET",
                           "/settings" if settings is not None else "/status",
                           json.dumps(settings).encode() if settings is not None else None,
                           {"Content-Type": "application/json", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(16385)
        if len(raw) > 16384:
            raise ControlError("宿主机网络管理响应超过大小限制")
        value = strict_json(raw)
        if response.status != 200:
            message = value.get("error") if isinstance(value, dict) else None
            raise ControlError(message if isinstance(message, str) else "宿主机网络管理操作失败",
                               response.status if response.status in (400, 409, 503) else 503)
        return validate_status(value)
    except ControlError:
        raise
    except (OSError, ValueError, http.client.HTTPException, RecursionError) as exc:
        raise ControlError("宿主机网络管理服务不可用；请检查是否已安装并启动端口管理服务") from exc
    finally:
        connection.close()
