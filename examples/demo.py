"""Generate synthetic text in a NEW isolated directory; never discover WeChat."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from backend import Store


def create_demo(parent=None):
    if parent is not None:
        parent = Path(parent).resolve(strict=True)
        if parent == ROOT or ROOT in parent.parents:
            raise ValueError("Demo files must remain outside the source tree.")
    workspace = Path(tempfile.mkdtemp(prefix="dsh-wechat-synthetic-", dir=parent))
    source = workspace / "synthetic-messages.jsonl"
    rows = []
    for i, (kind, label, body) in enumerate([
        ("friend", "示例好友", "请在周五前核对示例清单。"),
        ("group", "示例项目群", "示例同事负责演示，示例用户负责确认时间。"),
        ("official", "示例公众号", "示例文章卡片，不代表网页正文已读取。"),
    ], 1):
        rows.append({"account_id": "synthetic-account", "message_id": f"synthetic-message-{i}",
                     "conversation_id": f"synthetic-{kind}", "conversation_name": label,
                     "sender_id": f"synthetic-sender-{i}", "sender_name": "示例发言人",
                     "time": f"2026-01-01T09:0{i}:00+08:00", "text": body, "kind": kind})
    source.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    store = Store(workspace / "state")
    try:
        store.import_source(source)
        result = store.review({"start_date": "2026-01-01", "end_date": "2026-01-01", "kinds": ["friend", "group"]})
    finally:
        store.close()
    return {"synthetic": True, "messages_generated": len(rows), "selected_messages": len(result["messages"]),
            "wechat_accessed": False, "default_state_changed": False, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, help="Optional existing parent for a new synthetic directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(create_demo(args.parent), ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error": "SYNTHETIC_DEMO_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
