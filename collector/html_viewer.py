"""Create a self-contained, offline reader for exported WeChat records.

Only the supplied export data is read. This module never opens WeChat files,
contacts a server, fetches media, or executes text from a message.

"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path


_STYLE = """
:root{color-scheme:light;font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1b2533;background:#f5f7fa}
*{box-sizing:border-box}body{margin:0}button,input{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible{outline:3px solid #66a0ea;outline-offset:2px}
header{padding:18px 24px;background:#fff;border-bottom:1px solid #dce2eb}h1{font-size:21px;margin:0 0 8px}header p{margin:4px 0;font-size:13px;color:#556477;line-height:1.6}
.layout{display:grid;grid-template-columns:290px minmax(0,1fr);height:calc(100vh - 137px);min-height:400px}aside{padding:18px 12px;background:#fff;border-right:1px solid #dce2eb;overflow:auto}
label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input{width:100%;border:1px solid #bbc7d5;border-radius:6px;padding:10px;background:#fff;color:#1b2533}#conversations{margin-top:12px}
.conversation{display:block;width:100%;padding:11px 10px;border:0;border-radius:6px;background:transparent;text-align:left;margin-bottom:3px;color:inherit}.conversation:hover{background:#f0f4f9}.conversation[aria-current=true]{background:#e5efff}.conversation-name{display:block;overflow-wrap:anywhere}.conversation-count{display:block;color:#637187;font-size:12px;margin-top:4px}
main{overflow:auto;padding:22px 28px}.search{max-width:850px}.search-hint{font-size:12px;color:#637187;margin:7px 0 20px}h2{font-size:18px;margin:18px 0 7px;overflow-wrap:anywhere}#status{color:#637187;font-size:13px;margin-bottom:18px}.message{background:#fff;border:1px solid #dde4ed;border-radius:8px;padding:14px 16px;margin:0 0 12px;max-width:1000px}.meta{display:flex;gap:8px;flex-wrap:wrap;font-size:12px;color:#5f6e82;line-height:1.6}.sender{font-weight:650;color:#26384f}.message-content{font-size:14px;line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}.source{font-size:11px;color:#7a8797;overflow-wrap:anywhere;margin-top:10px}.empty{padding:30px 0;color:#637187;font-size:14px;line-height:1.7}.more{border:1px solid #bbc7d5;background:#fff;color:#26384f;border-radius:6px;padding:10px 24px;margin:6px 0 28px}#more[hidden]{display:none}.noscript{padding:18px;color:#9b281e}
@media(max-width:760px){header{padding:16px}h1{font-size:19px}.layout{grid-template-columns:1fr;height:auto}aside{border-right:0;border-bottom:1px solid #dce2eb;max-height:260px}main{padding:18px 16px;overflow:visible}.message{padding:12px}}
""".strip()

_SCRIPT = r"""
'use strict';
(() => {
  const data = JSON.parse(document.getElementById('export-data').textContent);
  const $ = (id) => document.getElementById(id);
  const conversationList = $('conversations');
  const messageList = $('messages');
  const conversationSearch = $('conversation-search');
  const fullSearch = $('full-search');
  const more = $('more');
  const pageSize = 200;
  let activeId = data.conversations.length ? data.conversations[0].id : null;
  let currentRows = [];
  let shown = 0;
  let timer = null;
  const names = new Map(data.conversations.map((c) => [c.id, c.name]));
  const allRows = [];
  for (const c of data.conversations) {
    for (const record of data.records[c.id] || []) {
      allRows.push({conversationId: c.id, record});
    }
  }
  const textElement = (tag, value, className) => {
    const el = document.createElement(tag);
    el.textContent = value == null ? '' : String(value);
    if (className) el.className = className;
    return el;
  };
  const updateConversations = () => {
    const query = conversationSearch.value.trim().toLocaleLowerCase();
    const fragment = document.createDocumentFragment();
    let matched = 0;
    for (const c of data.conversations) {
      if (query && !(c.name + ' ' + c.id).toLocaleLowerCase().includes(query)) continue;
      const button = textElement('button', '', 'conversation');
      button.type = 'button';
      button.setAttribute('aria-current', String(c.id === activeId && !fullSearch.value.trim()));
      button.append(textElement('span', c.name, 'conversation-name'));
      button.append(textElement('span', `${c.count.toLocaleString()} 条记录`, 'conversation-count'));
      button.addEventListener('click', () => {
        activeId = c.id;
        fullSearch.value = '';
        if (timer !== null) clearTimeout(timer);
        updateConversations();
        resetMessages();
      });
      fragment.append(button);
      matched += 1;
    }
    if (!matched) fragment.append(textElement('p', '没有匹配的会话。', 'empty'));
    conversationList.replaceChildren(fragment);
  };
  const appendPage = () => {
    const end = Math.min(shown + pageSize, currentRows.length);
    const fragment = document.createDocumentFragment();
    for (let i = shown; i < end; i += 1) {
      const {conversationId, record} = currentRows[i];
      const article = textElement('article', '', 'message');
      const meta = textElement('div', '', 'meta');
      meta.append(textElement('span', record.sender || '未知发送者', 'sender'));
      meta.append(textElement('span', record.time || '时间未解析'));
      meta.append(textElement('span', '类型：' + (record.type || '未知')));
      if (fullSearch.value.trim()) meta.append(textElement('span', '会话：' + (names.get(conversationId) || conversationId)));
      article.append(meta);
      article.append(textElement('div', record.content || '（此记录没有可显示的文本；媒体内容未还原）', 'message-content'));
      const origin = [];
      if (record.source_db) origin.push('来源：' + record.source_db);
      if (record.local_id) origin.push('记录 ID：' + record.local_id);
      if (origin.length) article.append(textElement('div', origin.join(' · '), 'source'));
      fragment.append(article);
    }
    messageList.append(fragment);
    shown = end;
    $('status').textContent = `共 ${currentRows.length.toLocaleString()} 条，已显示 ${shown.toLocaleString()} 条`;
    more.hidden = shown >= currentRows.length;
    more.textContent = `加载更多（下一批 ${Math.min(pageSize, currentRows.length - shown)} 条）`;
  };
  const resetMessages = () => {
    const query = fullSearch.value.trim().toLocaleLowerCase();
    if (query) {
      currentRows = allRows.filter(({record}) =>
        [record.content, record.sender, record.time, record.type].some((value) => value.toLocaleLowerCase().includes(query))
      );
      $('heading').textContent = '全部会话的搜索结果';
    } else {
      currentRows = activeId === null ? [] : (data.records[activeId] || []).map((record) => ({conversationId: activeId, record}));
      $('heading').textContent = activeId === null ? '暂无会话' : (names.get(activeId) || activeId);
    }
    shown = 0;
    messageList.replaceChildren();
    appendPage();
    if (!currentRows.length) messageList.append(textElement('p', query ? '没有找到匹配的记录。' : '此会话没有可显示的记录。', 'empty'));
  };
  conversationSearch.addEventListener('input', updateConversations);
  fullSearch.addEventListener('input', () => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => { updateConversations(); resetMessages(); }, 180);
  });
  more.addEventListener('click', appendPage);
  $('summary').textContent = `${data.conversations.length.toLocaleString()} 个会话 · ${allRows.length.toLocaleString()} 条已导出记录`;
  updateConversations();
  resetMessages();
})();
""".strip()

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-__SCRIPT_HASH__'; style-src 'sha256-__STYLE_HASH__'; connect-src 'none'; img-src 'none'; media-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>本地微信聊天记录</title>
<style>__STYLE__</style>
</head>
<body>
<header><h1>本地微信聊天记录</h1><p id="summary">正在读取导出记录…</p><p>离线浏览，仅展示本次导出的记录。图片、语音、视频、附件等非文本消息仅显示记录或描述，媒体内容未还原。</p></header>
<noscript><p class="noscript">此离线页面需要浏览器启用 JavaScript 才能搜索和显示记录。</p></noscript>
<div class="layout">
<aside aria-label="会话列表"><label for="conversation-search">查找会话</label><input id="conversation-search" type="search" autocomplete="off" placeholder="会话名称或标识"><nav id="conversations" aria-label="会话"></nav></aside>
<main><div class="search"><label for="full-search">全文搜索所有会话</label><input id="full-search" type="search" autocomplete="off" placeholder="搜索消息、发送者、时间或类型"><p class="search-hint">输入后搜索全部会话；清空搜索或点击左侧会话可返回会话记录。每次显示 200 条。</p></div><h2 id="heading"></h2><div id="status" aria-live="polite"></div><section id="messages" aria-label="聊天记录"></section><button id="more" class="more" type="button" hidden>加载更多</button></main>
</div>
<script id="export-data" type="application/json">__DATA__</script>
<script>__SCRIPT__</script>
</body>
</html>
"""


def _string(value: object) -> str:
    """Do not assign special meaning to anything provided in an export."""
    return "" if value is None else str(value)


def _script_json(value: object) -> str:
    # The HTML parser recognizes </script> even in application/json. Escape
    # every HTML-significant character before embedding, not just that token.
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in (("&", r"\u0026"), ("<", r"\u003c"), (">", r"\u003e"), ("\u2028", r"\u2028"), ("\u2029", r"\u2029")):
        encoded = encoded.replace(raw, escaped)
    return encoded


def _sha256_base64(value: str) -> str:
    return base64.b64encode(hashlib.sha256(value.encode("utf-8")).digest()).decode("ascii")


def write_viewer(
    output_dir: Path,
    conversations: list[dict],
    records_by_conversation: dict[str, list[dict]],
) -> Path:
    """Write ``index.html`` and return its path, using only supplied records.

    Conversation fields: id/name/count. Record fields: time/sender/type/content/
    source_db/local_id. All other fields are omitted. Record order is preserved;
    callers should supply the intended chronological order. Counts are derived
    from the actual exported records. The HTML is self-contained and prohibits
    external resource requests using its Content Security Policy.
    """
    normalized_conversations: list[dict] = []
    normalized_records: dict[str, list[dict]] = {}
    record_fields = ("time", "sender", "type", "content", "source_db", "local_id")
    for conversation in conversations:
        conversation_id = _string(conversation.get("id"))
        if conversation_id in normalized_records:
            raise ValueError(f"Duplicate conversation id: {conversation_id!r}")
        rows = records_by_conversation.get(conversation_id, [])
        normalized_records[conversation_id] = [
            {field: _string(record.get(field)) for field in record_fields}
            for record in rows
        ]
        normalized_conversations.append({
            "id": conversation_id,
            "name": _string(conversation.get("name")) or conversation_id or "未命名会话",
            "count": len(rows),
        })
    payload = _script_json({"conversations": normalized_conversations, "records": normalized_records})
    # Replace data last: user text resembling a template marker must remain data.
    page = (_HTML.replace("__SCRIPT_HASH__", _sha256_base64(_SCRIPT))
            .replace("__STYLE_HASH__", _sha256_base64(_STYLE))
            .replace("__STYLE__", _STYLE)
            .replace("__SCRIPT__", _SCRIPT)
            .replace("__DATA__", payload))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = output_dir / "index.html"
    with result.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(page)
    return result
