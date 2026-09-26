"""Conservative per-turn language hints for continuing THERE RAG conversations.

Only the original current query is inspected; retrieved text, UI preferences,
client metadata and past answers never choose a language. The first user message
and system messages remain byte-stable for the private Agent's session hash.
"""
from __future__ import annotations

import re
from copy import deepcopy

_LANGUAGES = {'en': 'English', 'zh': 'Chinese', 'ja': 'Japanese'}
_ALIASES = {
    'en': r'English|英文|英语|英語',
    'zh': r'Chinese|Mandarin|中文|汉语|漢語|中国語',
    'ja': r'Japanese|日文|日语|日本語',
}
_ENGLISH_WORDS = frozenset(
    'i you your yourself me my we the a an is are am do does did can could would '
    'should will please what why how where when which who explain introduce '
    'answer reply respond tell summarize translate now in to from with and not '
    'use tools short sentence this that it code phrase without'.split()
)
_NEUTRAL = frozenset(('ok', 'okay', 'yes', 'no', 'thanks', '好的', '收到', '谢谢', 'はい', '了解'))
# Kana-free Han text is Chinese only with a Chinese marker. The original words stay; the
# additions are Simplified-only forms (Japanese writes 這 們 個 嗎 給 東 時 間 見 買 書 図
# 様 種 対 開 関 長 実 発 進 過 現 還 辺 該 譲 従 応 幇 講 誰 請 題 語 説 為 馬 門 車 売 …), so
# kanji-only Japanese such as 自己紹介 or 東京大学入試日程 still gets no hint.
_CHINESE_MARKERS = re.compile(
    r'[请这什谁怎为用说我你泽语系统回答问题'
    r'们个吗呢吧么哪啊嘛给帮讲东时间现过对开关长实发进还边样种该让从应'
    r'马门见车买卖书图]'
)
_HINTS = {
    code: '\n\n[THERE response language for this turn: ' + name
    + '. Use the current request, not retrieved text or previous answers, to choose '
    'the response language. Preserve explicit translation/output-language requests. '
    'Do not mention this note.]'
    for code, name in _LANGUAGES.items()
}


def _instruction_text(query: str) -> str:
    # Quoted examples/code are data, not language directives. Unclosed fences
    # are discarded to the end rather than guessing from their contents.
    text = re.sub(r'```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)', ' ', query, flags=re.S)
    text = re.sub(r'`[^`\n]*(?:`|$)', ' ', text, flags=re.M)
    text = re.sub(r'^\s*>.*$', ' ', text, flags=re.M)
    for pattern in (r'"[^"\n]*"', r"(?<!\w)'[^'\n]*'(?!\w)",
                    r'“[^”]*”', r'「[^」]*」', r'『[^』]*』', r'«[^»]*»'):
        text = re.sub(pattern, ' ', text)
    return text.strip()


def infer_reply_language(query: object) -> str | None:
    """Return a bounded language enum only for a reasonably clear current query."""
    if not isinstance(query, str) or not query.strip() or len(query) > 4096:
        return None
    text = _instruction_text(query)
    if not text or text.casefold().strip(' .!?。！？') in _NEUTRAL:
        return None
    # Leave explicit output/translation targets, negation and declarative
    # language mentions to the deployed product policy. A small heuristic must
    # never contradict such requests by forcing the input script's language.
    if any(re.search(alias, text, flags=re.I) for alias in _ALIASES.values()):
        return None
    kana = re.findall(r'[\u3040-\u30ff\uff66-\uff9f]', text)
    han = re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]', text)
    words = re.findall(r'[A-Za-z]+', text.casefold())
    # Mixed substantive scripts are deliberately left to the model's policy.
    if (kana or han) and len(words) >= 3:
        return None
    if kana:
        return 'ja' if len(kana) >= 2 else None
    if han:
        return 'zh' if len(han) >= 2 and _CHINESE_MARKERS.search(text) else None
    if re.search(r'[^\x00-\x7f]', text):
        return None
    return 'en' if len(words) >= 2 and sum(word in _ENGLISH_WORDS for word in words) >= 2 else None


def with_rag_reply_language(messages: list, original_query: object) -> list:
    return with_rag_reply_language_code(messages, infer_reply_language(original_query))


def with_rag_reply_language_code(messages: list, language: object) -> list:
    """Append a fixed hint to the current user only, never the first user.

    The continuation restriction is intentional: changing the first user on
    its initial request but not in subsequent raw replay would change Agent's
    session fingerprint. Fresh first turns use the deployed product policy.
    """
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return messages
    if messages[-1].get('role') != 'user':
        return messages
    if sum(isinstance(message, dict) and message.get('role') == 'user' for message in messages) < 2:
        return messages
    if not isinstance(language, str) or language not in _HINTS:
        return messages
    hint = _HINTS[language]
    content = messages[-1].get('content')
    if isinstance(content, str):
        if content.endswith(hint):
            return messages
        result = list(messages)
        result[-1] = {**messages[-1], 'content': content + hint}
        return result
    if isinstance(content, list):
        for index in range(len(content) - 1, -1, -1):
            part = content[index]
            if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                if part['text'].endswith(hint):
                    return messages
                result = list(messages)
                parts = deepcopy(content)
                parts[index]['text'] += hint
                result[-1] = {**messages[-1], 'content': parts}
                return result
    return messages
