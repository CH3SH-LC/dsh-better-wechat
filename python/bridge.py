"""One request on stdin, exactly one privacy-filtered JSON response on stdout."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from backend import Store, SafeError, TZ, dumps, error_response, private_json


PACKAGE_VERSION = json.loads((Path(__file__).resolve().parents[1] / "package.json").read_text(encoding="utf-8"))["version"]

RUNTIME_TOOLS = ["wechat_status", "wechat_prepare", "wechat_refresh", "wechat_search",
                 "wechat_context", "wechat_review", "wechat_save_tasks", "wechat_list_tasks",
                 "wechat_conversations", "wechat_get_preferences", "wechat_set_preferences"]

RECEIPT_NAME = "plugin-runtime.json"


def receipt_tools(params):
    """Accept a non-empty, duplicate-free subset of the known tool names.

    The plugin now registers its tools from several independent components, so a
    receipt may legitimately cover a functional group instead of every tool. An
    unknown, empty or repeated name is still a wiring error, and anything outside
    the fixed key set is still rejected before this is called.
    """
    tools = params["tools"]
    if type(tools) is not list or not tools or len(tools) > len(RUNTIME_TOOLS):
        raise SafeError("invalid_request")
    if any(type(name) is not str or name not in RUNTIME_TOOLS for name in tools):
        raise SafeError("invalid_request")
    if len(set(tools)) != len(tools):
        raise SafeError("invalid_request")
    return tools


def loaded_tools(store, host_pid):
    """Tools already recorded by *the same* host process, for the union receipt.

    A receipt left by another process (or any unreadable/foreign receipt) never
    contributes: a restarted host must not inherit tools it did not register.
    """
    path = store.private_file(RECEIPT_NAME)
    if not path.is_file():
        return []
    try:
        previous = private_json(path, max_bytes=64 * 1024)
    except Exception:
        return []
    if not isinstance(previous, dict) or previous.get("host_pid") != host_pid:
        return []
    tools = previous.get("tools")
    if type(tools) is not list:
        return []
    return [name for name in tools if type(name) is str and name in RUNTIME_TOOLS]


def runtime_receipt(store, request):
    """Internal host-load diagnostic, deliberately absent from model dispatch."""
    params = request.get("params")
    if (set(request) != {"action", "params"} or not isinstance(params, dict)
            or set(params) != {"version", "tools", "host_pid"}
            or params["version"] != PACKAGE_VERSION
            or type(params["host_pid"]) is not int or not 1 <= params["host_pid"] <= 2**32 - 1):
        raise SafeError("invalid_request")
    tools = receipt_tools(params) + loaded_tools(store, params["host_pid"])
    # A subset of RUNTIME_TOOLS keeps declaration order, so the union is ordered
    # too and never depends on which component loaded first.
    union = [name for name in RUNTIME_TOOLS if name in set(tools)]
    store.write_private_json(RECEIPT_NAME, {
        "version": PACKAGE_VERSION, "tools": union,
        "loaded_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "host_pid": params["host_pid"],
    })
    return {"recorded": True}



class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SafeError("invalid_request")


def main():
    store = None
    try:
        parser = Parser(add_help=False)
        parser.add_argument("--state", required=True)
        args = parser.parse_args()
        line = sys.stdin.buffer.readline(128 * 1024 + 1)
        if not line or len(line) > 128 * 1024:
            raise SafeError("invalid_request")
        request = json.loads(line.decode("utf-8"))
        store = Store(args.state)
        data = runtime_receipt(store, request) if isinstance(request, dict) and request.get("action") == "runtime_receipt" else store.dispatch(request)
        result = {"ok": True, "data": data}
    except KeyboardInterrupt:
        result = error_response(SafeError("cancelled"))
    except BaseException as exc:
        result = error_response(exc)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    sys.stdout.buffer.write((dumps(result) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
