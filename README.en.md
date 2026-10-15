# dsh-better-wechat

[中文说明（推荐阅读）](README.md) · [Architecture](docs/ARCHITECTURE.md) · [Privacy](PRIVACY.md) · [Contributing](CONTRIBUTING.md)

Turn the WeChat conversations on your own computer into a private, searchable message workspace for
[DeepSeek Harness](https://github.com/deepseek-ai) (DSH). Collection, indexing and search all happen
locally — your chat content never leaves your machine.

This is an independent project, not an official WeChat or DeepSeek product.

## What it gives you

- **Ask for a message in plain language** — by person, group, date or keyword, with original text and context.
- **Get drafts of what you owe people** — promises, owners and deadlines collected from your chats, each tied to its source message.
- **Ignore muted groups in one word** — skip the noisy groups you already silenced; unclear state is never treated as "not muted".
- **Stay local** — no uploads, no message sending, no writing to WeChat, no reading of image, voice, video or file bodies.

## Getting started

Requirements: DSH with workspace bundle support, Python 3.10+. Automatic reading of the local WeChat
client requires Windows, 64-bit Python and a logged-in WeChat 4.x. Importing an existing chat export
works on macOS and Linux too.

1. Install the plugin directory through DSH's local plugin page (or `install_bundle`), then **fully
   restart DSH** — refreshing a page or toggling the plugin does not reload plugin code.
2. Ask DSH to initialize the WeChat assistant. It reads the local client once, verifies the copies and
   builds a local index.
3. Start asking, for example: "what do I need to reply to today?", or "organize today's to-dos, ignore
   muted groups".

After that, one "refresh" request a day is enough. Existing exports can be connected instead of a local
client. For setup, refresh, account selection and source selection from the command line:

```text
python setup.py --doctor --json
python setup.py --install-collector-dependencies
python setup.py --refresh --json
python setup.py --review --start-date 2026-01-01 --end-date 2026-01-01 --kind group --json
```

Dependency installation is the only explicit network operation. To try synthetic data without WeChat or DSH:

```text
python examples/demo.py
```

## Honest boundaries

Reading works on a local snapshot, not live chat, and an empty result never proves that a day had no
messages. Image, voice, video and file bodies are not decoded; link cards are not article bodies.
Candidate tasks are drafts that need human review. A successful snapshot does not prove complete phone
history. Remote models receive the selected text that you authorize, so analysis itself is not
necessarily offline.

## Build and release

```text
python -m pip install -r collector/requirements.txt
python tools/check.py
python tools/release.py
```

The build uses an explicit source-file allowlist, privacy checks before publication, deterministic ZIP
metadata, per-file hashes and SHA256SUMS. It does not publish remotely. See
[release instructions](docs/RELEASING.md); do not upload private captures or a containing workspace.

Original project components use MIT. The attributed capture implementation uses Apache-2.0; see
[third-party notices](THIRD_PARTY_NOTICES.md) and `LICENSES/Apache-2.0.txt`.
