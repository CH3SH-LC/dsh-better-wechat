"""Fixed full import/capture for stage one; selections belong to text queries."""
import sys

from backend import Store, SafeError, dumps, error_response
from bridge import Parser


def main():
    store = None
    try:
        parser = Parser(add_help=False)
        sources = parser.add_mutually_exclusive_group(required=True)
        sources.add_argument("--source")
        sources.add_argument("--latest-root")
        sources.add_argument("--native", action="store_true")
        parser.add_argument("--state", required=True)
        parser.add_argument("--configure-only", action="store_true")
        parser.add_argument("--account-dir")
        args = parser.parse_args()
        if args.account_dir and (not args.native or args.configure_only):
            raise SafeError("invalid_request")
        store = Store(args.state)
        if args.source and not args.configure_only:
            data = store.import_source(args.source)
        else:
            mode = "snapshot" if args.source else "latest_complete" if args.latest_root else "native"
            if args.configure_only:
                data = store.configure_source(args.source or args.latest_root, mode)
            else:
                data = store.refresh_from(args.source or args.latest_root, mode, collector_account_dir=args.account_dir)
        result = {"ok": True, "data": data}
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
