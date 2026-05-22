#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import difflib
import getpass
import json
import math
import os
import re
import sys
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests
from colorama import Fore, Style, init as colorama_init


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/owl-alpha"
HTTP_REFERER = "http://localhost"
APP_TITLE = "Cortex Code"
CONFIG_VERSION = 1

SALMON = "\033[38;2;229;152;145m"
SALMON_DIM = "\033[38;2;180;110;108m"
RESET_STYLE = Style.RESET_ALL

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "chat_history.json"  
CHATS_DIR = APP_DIR / "chats"
CHATS_INDEX_FILE = APP_DIR / "chats_index.json"
MAX_CONTEXT_TOKENS = 200_000
CHARS_PER_TOKEN = 4  


PARTIAL_EDIT_API_MAX_TOKENS = 1024  
BLOATED_PARTIAL_REPLY_TOKENS = 600  
PARTIAL_EDIT_TARGET_OUTPUT = "50–500"  
DIFF_MAX_LINES = 48  

MULTILINE_END_TRIPLE = chr(34) * 3
MULTILINE_END_FENCE = "```"

FILE_BLOCK_PATTERN = re.compile(
    r"<<<FILE:(?P<path>[^>]+)>>>\s*\n(?P<content>.*?)<<<END>>>",
    re.DOTALL,
)
REPLACE_BLOCK_PATTERN = re.compile(
    r"<<<REPLACE:(?P<path>[^>]+)>>>\s*\n(?P<body>.*?)<<<END>>>",
    re.DOTALL,
)
APPEND_BLOCK_PATTERN = re.compile(
    r"<<<APPEND:(?P<path>[^>]+)>>>\s*\n(?P<content>.*?)<<<END>>>",
    re.DOTALL,
)
FILE_BLOCK_LOOSE_PATTERN = re.compile(
    r"<<<FILE:(?P<path>[^>]+)>>>\s*\n(?P<content>.*?)(?=<<<FILE:|<<<END>>>|\Z)",
    re.DOTALL,
)
CD_BLOCK_PATTERN = re.compile(r"<<<CD:(?P<path>[^>]+)>>>", re.IGNORECASE)


FAKE_PATH_RE = re.compile(
    r"путь|path|folder|папк|example|новый|your|here|\.\.\.|\/\/",
    re.IGNORECASE,
)

LEAKED_PROMPT_RE = re.compile(
    r"(?:Если в сообщении уже есть содержимое|Не просите показать файл|"
    r"Пример использования:|Правила:\s*\n[-]+|Смена папки:\s*\n[-]+).*",
    re.DOTALL | re.IGNORECASE,
)

_WIN_SEG = r"[^\\/:*?\"<>|\r\n ]+"

DIR_REQUEST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:работай|перейди|создай|сделай|пиши|сохрани|используй)\s+"
        r"(?:мне\s+)?(?:в|во|на)\s+"
        r"(?:папк[еуи]|директори[июе]|каталог[еу]?|folder|directory)\s+"
        r'["\']?(?P<path>.+?)["\']?\s*$',
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:в|во|на)\s+(?:папк[еуи]|директори[июе]|каталог[еу]?)\s+"
        r'["\']?(?P<path>.+?)["\']?\s*$',
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:в|во|in|to|at)\s+(?P<path>[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<path>[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})\s+"
        r"в\s+(?:этой|этом)\s*(?:папк[еуи]|директори[июе]|дериктори[июе]|каталог[еу]?)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<path>[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})\s+"
        r"(?:сделай|создай|напиши|сгенерируй|создать|сделать)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<path>[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})\s+"
        r"(?:добавь|добавить|исправь|исправить|измени|изменить|обнови|обновить|"
        r"дополни|дополнить|fix|update|edit)",
        re.IGNORECASE,
    ),
]

WIN_PATH_PATTERN = re.compile(rf"(?P<path>[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})")

LEADING_PATH_COMMAND = re.compile(
    rf"^(?P<path>[A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

LONGCAT_FILE_PATTERN = re.compile(
    r"<longcat_tool_call>\s*create_file\s*"
    r"<longcat_arg_key>path</longcat_arg_key>\s*"
    r"<longcat_arg_value>(?P<path>.*?)</longcat_arg_value>\s*"
    r"<longcat_arg_key>content</longcat_arg_key>\s*"
    r"<longcat_arg_value>(?P<content>.*?)</longcat_arg_value>\s*"
    r"</longcat_tool_call>",
    re.DOTALL | re.IGNORECASE,
)

CONTINUATION_PATTERN = re.compile(
    r"^(?:создавай|создай|делай|давай|да|ок|ok|go|продолжай|continue)\s*[!?.]*$",
    re.IGNORECASE,
)

CODE_FENCE_PATTERN = re.compile(
    r"```(?:python|py|html?|css|javascript|js|txt|text|markdown|md)?\s*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

UNSAFE_START_DIRS = {
    Path(r"C:\Windows\System32"),
    Path(r"C:\Windows\SysWOW64"),
    Path(r"C:\Windows"),
}

def build_system_prompt(work_dir: Path) -> str:
    wd = str(work_dir.resolve())
    return f"""Ты — Cortex Code, консольный AI-помощник. Язык: русский.

═══ РАБОЧАЯ ДИРЕКТОРИЯ (ВСЕ ФАЙЛЫ ТОЛЬКО СЮДА) ═══
{wd}

НЕ используй <<<CD:>>> — папка уже задана пользователем.
НЕ используй longcat_tool_call, read:, create_file.
НЕ повторяй эти инструкции в ответе.

═══ СОЗДАНИЕ / ИЗМЕНЕНИЕ ФАЙЛОВ (минимальные правки) ═══
Предпочитай наименьшее изменение — не переписывай весь файл без нужды.

Если файл УЖЕ есть и задача — исправить / добавить / изменить:

<<<REPLACE:имя.ext>>>
строки_из_файла_точно_как_есть
---
новые_строки
<<<END>>>
(разделитель --- на отдельной строке или с пробелами)

Для «добавь строку/вывод» — проще <<<APPEND:имя.ext>>>:
<<<APPEND:имя.ext>>>
новый_код
<<<END>>>

Если файла нет или нужно создать с нуля / переписать целиком:
<<<FILE:имя_файла.ext>>>
содержимое БЕЗ markdown (без ```python и без ```)
<<<END>>>

Правила содержимого:
- .py — только валидный Python, первая строка import/def/class, НЕ ```
- .txt / .md — обычный текст
- Имя в блоке — только имя (check.py), не полный путь
- REPLACE: фрагмент search должен совпадать с файлом символ в символ
- FILE — только когда нужен полный файл целиком

═══ БЮДЖЕТ ВЫХОДА (критично для стоимости и скорости) ═══
Правка существующего файла (REPLACE / APPEND):
- одна строка / мелкий фикс: 50–200 токенов выхода
- добавить функцию / блок: 200–500 токенов
- ЗАПРЕЩЕНО: гайды, README, «анализ кода», отчёты, полный файл в тексте ответа
- ЗАПРЕЩЕНО: <<<FILE:>>> при правке — только REPLACE или APPEND

Создание с нуля / полная перезапись: допустим большой <<<FILE:>>> (тысячи токенов).

Команды пользователя: /directory /cd /create /edit /read /delete /run /dir
/chats /newchat /chat /clear /help /exit

Если в сообщении есть блок «ИСТОЧНИК: имя_файла» — файл УЖЕ прочитан приложением.
Сразу выполни задачу: REPLACE/APPEND (правка) или <<<FILE:>>> (создание). Не пиши «сначала прочитаю».

═══ ЗАПРЕТ ОТЛОЖЕННЫХ ДЕЙСТВИЙ (ОБЯЗАТЕЛЬНО) ═══
НИКОГДА не пиши: «создаю», «сейчас создам», «готовлю файл», «пишу код», «подожди»,
«сделаю в следующем сообщении», «Cortex: Создаю…».
Сначала блок REPLACE/APPEND/FILE, потом максимум одно короткое предложение итога (не абзацы текста).
"""


def build_lite_edit_system_prompt(work_dir: Path, filename: str) -> str:
    wd = str(work_dir.resolve())
    return f"""Ты — Cortex Code. Режим точечной правки одного файла. Язык: русский.

WORK_DIR: {wd}
Файл: {filename}

Ответ ТОЛЬКО одним блоком.

Замена (обязателен разделитель --- между старым и новым фрагментом):
<<<REPLACE:{filename}>>>
старые_строки_из_файла_как_есть
---
новые_строки
<<<END>>>

Добавить строки (проще для «добавь print…»):
<<<APPEND:{filename}>>>
print("made by snezy")
<<<END>>>

ЗАПРЕЩЕНО: <<<FILE:{filename}>>>, только новый код без --- в REPLACE, гайды, markdown ```.
Вне блока — максимум 1 короткое предложение. Без «создаю», «подожди».
Бюджет выхода: {PARTIAL_EDIT_TARGET_OUTPUT} токенов."""


def build_lite_edit_messages(
    user_text: str,
    filename: str,
    fm: FileManager,
    prepared_files: list[str],
    extra: str,
) -> list[dict[str, str]]:
    parts = [user_text.strip()]
    primary = filename
    to_embed = [primary] if primary else []
    for name in prepared_files:
        if name not in to_embed:
            to_embed.append(name)
    for fname in to_embed:
        content = fm.read_file(fname)
        if content.startswith("Ошибка"):
            parts.append(f"\n[Файл {fname} не найден в {fm.work_dir}]")
            continue
        tag = _lang_tag_for_file(fname)
        parts.append(
            f"\n═══ ФАЙЛ {fname} (правь минимально, только REPLACE/APPEND) ═══\n"
            f"```{tag}\n{content}\n```"
        )
    body = "\n".join(parts) + extra
    return [
        {"role": "system", "content": build_lite_edit_system_prompt(fm.work_dir, primary)},
        {"role": "user", "content": body},
    ]


EDIT_REQUEST_PATTERN = re.compile(
    r"(?:добавь|добавить|исправь|исправить|обнови|обновить|измени|изменить|"
    r"дополни|дополнить|переделай|улучши|расширь|fix|update|edit|add to)",
    re.IGNORECASE,
)

CREATE_REWRITE_PATTERN = re.compile(
    r"(?:создай|создать|перепиши|переписать|с нуля|заново|rewrite|recreate|create)",
    re.IGNORECASE,
)

LONGCAT_READ_STUB = re.compile(
    r"<longcat_tool_call>\s*read:?\s*[^<\n]+|"
    r"Покажи содержимое[^.\n]*(?:файл[а]?)?[^.\n]*\.?",
    re.IGNORECASE,
)

READ_PROMISE_STUB = re.compile(
    r"(?:Сначала|Сейчас|Для начала|Нужно|Мне нужно)\s+"
    r"(?:прочитаю|прочитать|открою|посмотрю|ознакомлюсь|возьму)[^.!\n]*[.!]?\s*",
    re.IGNORECASE,
)

FILE_PROMISE_STUB = re.compile(
    r"(?:"
    r"(?:Cortex\s*:\s*)?"
    r"(?:Сейчас|Сначала|Для начала|Скоро|Подожди|Окей|Хорошо|Далее|Давайте?)\s*,?\s*"
    r")?"
    r"(?:"
    r"(?:создаю|создам|создать|пишу|напишу|готовлю|формирую|делаю|сделаю|"
    r"генерирую|сгенерирую|обновляю|обновлю|исправляю|исправлю|добавляю|добавлю)"
    r"(?:\s+(?:для вас|тебе|вам|сейчас))?"
    r"(?:\s+(?:файл|файлы|код|скрипт|сайт|html|гайд|страницу))?"
    r"[^.\n]*[.!]?\s*"
    r")+",
    re.IGNORECASE,
)

DEFER_ACTION_STUB = re.compile(
    r"(?:"
    r"(?:в|на)\s+следующ(?:ем|ий)\s+(?:сообщени|шаг|ответ)|"
    r"сейчас\s+(?:начну|приступлю|займусь)|"
    r"подожди(?:те)?\s*(?:немного|чуть|секунду)?|"
    r"скоро\s+(?:будет|готово|сделаю)"
    r")[^.\n]*[.!]?\s*",
    re.IGNORECASE,
)

SOURCE_FILE_HINT = re.compile(
    r"(?:из|from|по|на основе|возьми|взять|используй|используя|прочитай|прочитать|"
    r"содержимое|текст(?:ом)?)\s+(?:файл[а]?\s+)?[\"']?([a-zA-Z0-9_\-.]+\.[a-zA-Z0-9]+)",
    re.IGNORECASE,
)


def sanitize_file_content(content: str) -> str:
    text = content.strip("\ufeff")
    wrapped = re.match(
        r"^```(?:python|py|txt|text|markdown|md)?\s*\r?\n(.*)\r?\n```\s*$",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if wrapped:
        text = wrapped.group(1)
    lines = text.splitlines()
    while lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def is_real_windows_path(path: str) -> bool:
    path = path.strip()
    if not re.match(r"^[A-Za-z]:\\", path):
        return False
    if FAKE_PATH_RE.search(path):
        return False
    bad_parts = {
        "путь", "к", "папке", "новый", "path", "folder", "your", "here",
        "директории", "directory",
    }
    for part in path.lower().split("\\"):
        if part in bad_parts:
            return False
    return True


def is_safe_filename(name: str) -> bool:
    name = Path(name.strip()).name
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if FAKE_PATH_RE.search(name):
        return False
    return bool(re.match(r"^[\w\-. ]+\.[a-z0-9]{1,8}$", name, re.IGNORECASE))


def safe_file_ref(path: str, work_dir: Path) -> str | None:
    raw = path.strip().strip('"\'')
    p = Path(raw)
    if p.is_absolute():
        if is_real_windows_path(str(p)):
            try:
                raw = str(p.resolve().relative_to(work_dir.resolve()))
            except ValueError:
                raw = p.name
        else:
            raw = p.name
    if ".." in raw.replace("\\", "/"):
        return None
    if not is_safe_filename(Path(raw).name):
        return None
    return raw.replace("\\", "/")


def strip_ai_noise(text: str) -> str:
    text = LEAKED_PROMPT_RE.sub("", text)
    text = READ_PROMISE_STUB.sub("", text)
    text = FILE_PROMISE_STUB.sub("", text)
    text = DEFER_ACTION_STUB.sub("", text)
    text = re.sub(r"<longcat_tool_call>.*?(?:</longcat_tool_call>|$)", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<<<CD:[^>]+>>>", "", text, flags=re.I)
    return text


def clean_ai_visible(text: str) -> str:
    text = strip_ai_noise(text)
    text = re.sub(r"<<<FILE:[^>]+>>>\s*\n.*?<<<END>>>", "", text, flags=re.DOTALL)
    text = re.sub(r"<<<REPLACE:[^>]+>>>\s*\n.*?<<<END>>>", "", text, flags=re.DOTALL)
    text = re.sub(r"<<<APPEND:[^>]+>>>\s*\n.*?<<<END>>>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:html?|css|javascript|js|python|py)?\s*\r?\n.*?```", "", text, flags=re.DOTALL | re.I)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines).strip()


FALSE_FILE_CLAIM_RE = re.compile(
    r"(?:файл|file)\s*[`'\"]?\S+[`'\"]?\s*(?:уже\s+)?(?:создан|записан|сохранён|готов|saved|created)|"
    r"(?:создан|записан|сохранён)\s+(?:файл|file)|"
    r"(?:лендинг|сайт|страниц[ау])\s+(?:создан|готов)",
    re.IGNORECASE,
)


def finalize_visible_reply(
    visible: str,
    fn: str,
    needs_file: bool,
    file_saved: bool,
    partial_edit: bool = False,
) -> str:
    if needs_file and not file_saved:
        if FALSE_FILE_CLAIM_RE.search(visible):
            visible = FALSE_FILE_CLAIM_RE.sub("", visible).strip()
        if partial_edit:
            hint = (
                f"Файл {fn} НЕ изменён — нужен <<<REPLACE:{fn}>>> или <<<APPEND:{fn}>>>.\n"
                f"Повторите запрос (короткая правка, без гайда)."
            )
        else:
            hint = (
                f"Файл {fn} НЕ записан на диск — модель не прислала блок <<<FILE:{fn}>>>…<<<END>>>.\n"
                f"Повторите запрос или: /create {fn}"
            )
        return hint if not visible else f"{hint}\n\n{visible}"
    return visible


def response_has_file_block(text: str, filename: str | None = None) -> bool:
    if (
        FILE_BLOCK_PATTERN.search(text)
        or FILE_BLOCK_LOOSE_PATTERN.search(text)
        or REPLACE_BLOCK_PATTERN.search(text)
        or APPEND_BLOCK_PATTERN.search(text)
    ):
        return True
    if filename and CODE_FENCE_PATTERN.search(text):
        return True
    return bool(LONGCAT_FILE_PATTERN.search(text))


def _lang_tag_for_file(fname: str) -> str:
    ext = Path(fname).suffix.lower()
    return {".html": "html", ".htm": "html", ".css": "css", ".js": "javascript", ".py": "python"}.get(
        ext, "text"
    )


def mention_all_files(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]{1,8})", text, re.I):
        name = m.group(1)
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
    for m in SOURCE_FILE_HINT.finditer(text):
        name = m.group(1)
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def should_auto_read_sources(text: str) -> bool:
    return bool(
        re.search(
            r"из\s+\S+\.|возьми|взять|прочитай|на основе|содержим|текст|что в|"
            r"объясни|расскажи|опиши|создай|сделай|добавь|добавить|исправь|измени|"
            r"дополни|обнови|fix|edit|html|сайт|гайд|guide|\.txt|\.html|\.py",
            text,
            re.I,
        )
    )


def collect_context_files(
    text: str, fm: FileManager, session: SessionContext
) -> list[str]:
    target = extract_target_filename(text)
    target_key = target.lower() if target else None
    is_edit = bool(EDIT_REQUEST_PATTERN.search(text))

    if not should_auto_read_sources(text):
        result = detect_files_for_context(text, session, fm)
        if target and is_edit and target not in result:
            if fm._resolve(target).is_file():
                result.insert(0, target)
        return result

    result: list[str] = []

    if target and is_edit and target_key:
        if fm._resolve(target).is_file() and target not in result:
            result.append(target)

    for name in mention_all_files(text):
        path = fm._resolve(name)
        if not path.is_file():
            continue
        if target_key and name.lower() == target_key:
            if name not in result:
                result.append(name)
            continue
        if name not in result:
            result.append(name)

    if session.last_file and session.last_file not in result:
        p = fm._resolve(session.last_file)
        if p.is_file() and EDIT_REQUEST_PATTERN.search(text):
            result.append(session.last_file)

    return result


def extract_target_filename(text: str) -> str | None:
    m = re.search(
        r"(?:\bв|\bво|into|to)\s+([a-zA-Z0-9_\-.]+\.[a-zA-Z0-9]+)",
        text,
        re.I,
    )
    if m and is_safe_filename(m.group(1)):
        return m.group(1)
    t = text.lower()
    if re.search(r"html|сайт|гайд|guide", t):
        for m in re.finditer(r"([a-zA-Z0-9_\-]+\.html?)", text, re.I):
            if is_safe_filename(m.group(1)):
                return m.group(1)
    m = re.search(
        r"(?:создай|создать|напиши|сделай)\s+([a-zA-Z0-9_\-.]+\.[a-zA-Z0-9]+)",
        text,
        re.I,
    )
    if m and is_safe_filename(m.group(1)):
        return m.group(1)
    for m in re.finditer(r"([a-zA-Z0-9_\-.]+\.(?:py|txt|md|css|js))", text, re.I):
        if is_safe_filename(m.group(1)):
            return m.group(1)
    return None


def prefers_partial_edit(text: str, filename: str, fm: FileManager) -> bool:
    if not is_safe_filename(filename):
        return False
    if not fm._resolve(filename).is_file():
        return False
    if CREATE_REWRITE_PATTERN.search(text):
        return False
    return bool(EDIT_REQUEST_PATTERN.search(text))


def response_has_patch_block(text: str) -> bool:
    return bool(REPLACE_BLOCK_PATTERN.search(text) or APPEND_BLOCK_PATTERN.search(text))


def response_has_full_file_block(text: str) -> bool:
    return bool(FILE_BLOCK_PATTERN.search(text) or FILE_BLOCK_LOOSE_PATTERN.search(text))


def reply_output_estimate(text: str) -> int:
    return estimate_tokens(text)


def classify_reply_mode(text: str, partial_edit: bool) -> str:
    est = reply_output_estimate(text)
    if response_has_patch_block(text):
        return f"умная правка (REPLACE/APPEND), ~{est} tok"
    if response_has_full_file_block(text):
        return f"полный FILE, ~{est} tok"
    if partial_edit and est > BLOATED_PARTIAL_REPLY_TOKENS:
        return f"раздутый ответ без патча, ~{est} tok — не умное редактирование"
    return f"текст без файловых блоков, ~{est} tok"


def should_retry_bloated_partial_edit(
    reply: str, partial_edit: bool, file_saved: bool
) -> bool:
    if not partial_edit:
        return False
    if response_has_patch_block(reply):
        return False
    est = reply_output_estimate(reply)
    if est <= BLOATED_PARTIAL_REPLY_TOKENS and file_saved:
        return False
    return est > BLOATED_PARTIAL_REPLY_TOKENS or (
        response_has_full_file_block(reply) and not file_saved
    )


def wants_any_file(text: str) -> bool:
    return bool(
        re.search(
            r"\.py|\.txt|\.md|\.html|создай|создать|файл|напиши|скрипт|код|сайт|гайд|file|html",
            text,
            re.I,
        )
    )


class CortexPipeline:

    TOTAL_STAGES = 5

    def __init__(self) -> None:
        self._bar = ThinkingProgress("Cortex")

    def _println(self, text: str) -> None:
        print(f"{SALMON_DIM}  {text}{RESET_STYLE}")

    def stage(self, num: int, title: str, thought: str = "") -> None:
        self._println(f"[{num}/{self.TOTAL_STAGES}] {title}")
        if thought:
            self._println(f"    → {thought}")

    def thought(self, text: str) -> None:
        self._println(f"    → {text}")

    def done(self, summary: str) -> None:
        self._println(f"✓ {summary}")

    @contextmanager
    def wait_api(self, caption: str = "ожидание ответа модели…") -> Iterator[None]:
        self._bar.set_caption(caption)
        self._bar.start()
        try:
            yield
        finally:
            self._bar.stop(success=True)


class ThinkingProgress:
    BAR_WIDTH = 30
    SPINNER = ("|", "/", "-", "\\")
    TICK = 0.07

    def __init__(self, label: str = "Cortex") -> None:
        self.label = label
        self._caption = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._progress = 0.0
        self._frame = 0
        self._use_unicode = (getattr(sys.stdout, "encoding", None) or "").lower().startswith("utf")

    def set_caption(self, caption: str) -> None:
        self._caption = caption.strip()

    def _bar_chars(self) -> tuple[str, str, str, str]:
        if self._use_unicode:
            return ("█", "░", "│", "OK")
        return ("#", ".", "|", "OK")

    def _render(self, final: bool = False) -> str:
        full, empty, border, done = self._bar_chars()
        progress = 100.0 if final else self._progress
        spinner = done if final else self.SPINNER[self._frame % len(self.SPINNER)]
        fill = self.BAR_WIDTH if final else int(self.BAR_WIDTH * progress / 100)
        cap = ""
        if self._caption and not final:
            short = self._caption[:52] + ("…" if len(self._caption) > 52 else "")
            cap = f" {SALMON_DIM}| {short}{RESET_STYLE}"
        return (
            f"\r{SALMON}{self.label} {spinner} {SALMON_DIM}{border}{RESET_STYLE}"
            f"{SALMON}{full * fill}{SALMON_DIM}{empty * (self.BAR_WIDTH - fill)}{RESET_STYLE}"
            f"{SALMON_DIM}{border}{RESET_STYLE} {SALMON}{int(progress):3d}%{RESET_STYLE}{cap}"
        )

    def _write(self, text: str) -> None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "utf-8"
            sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))
            sys.stdout.flush()

    def _animate(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(self.TICK):
            self._progress = min(93.0, 94 * (1 - math.exp(-(time.monotonic() - start) / 7.5)))
            self._frame += 1
            try:
                self._write(self._render())
            except OSError:
                break

    def start(self) -> None:
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self, success: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            if success:
                self._write(self._render(final=True))
                time.sleep(0.12)
            self._write("\r\033[K")
        except OSError:
            pass


REPLACE_SEPARATORS = (
    "\n---\n",
    " --- ",
    "\n--- ",
    " ---\n",
    " => ",
    " -> ",
)

INSERT_AFTER_RE = re.compile(
    r"(?:после|after)\s+(?:строки\s+)?[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def parse_replace_parts(body: str) -> tuple[str, str] | None:
    text = body.strip("\n\r")
    if not text:
        return None
    for sep in REPLACE_SEPARATORS:
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip("\n"), right.strip("\n")
    if re.search(r"^---\s*$", text, re.MULTILINE):
        chunks = re.split(r"^---\s*$", text, maxsplit=1, flags=re.MULTILINE)
        if len(chunks) == 2:
            return chunks[0].strip("\n"), chunks[1].strip("\n")
    return None


def insert_after_anchor(content: str, anchor: str, snippet: str) -> str | None:
    pos = content.find(anchor)
    if pos < 0:
        return None
    line_end = content.find("\n", pos + len(anchor))
    if line_end < 0:
        line_end = len(content)
    ins = snippet if snippet.endswith("\n") else snippet + "\n"
    if not ins.startswith("\n"):
        ins = "\n" + ins
    return content[:line_end] + ins + content[line_end:]


def insert_before_marker(content: str, marker: str, snippet: str) -> str | None:
    pos = content.find(marker)
    if pos < 0:
        return None
    line = snippet.strip()
    if not line:
        return None
    if re.match(r"^\s+", snippet):
        ins = snippet.rstrip() + "\n"
    else:
        indent_m = re.match(r"^(\s*)", marker)
        indent = indent_m.group(1) if indent_m else ""
        ins = f"{indent}{line}\n"
    return content[:pos] + ins + content[pos:]


def _insert_in_function(
    content: str, func_name: str, snippet: str, markers: tuple[str, ...]
) -> str | None:
    m = re.search(rf"def {re.escape(func_name)}\s*\([^)]*\)\s*:", content)
    if not m:
        return None
    tail = content[m.start() :]
    line = snippet.strip()
    for marker in markers:
        pos = tail.find(marker)
        if pos >= 0:
            return insert_before_marker(content, marker, line)
    return insert_after_anchor(content, f"def {func_name}():", snippet)


def smart_insert_snippet(content: str, snippet: str, user_text: str) -> str | None:
    m = INSERT_AFTER_RE.search(user_text)
    if m:
        hit = insert_after_anchor(content, m.group(1), snippet)
        if hit is not None:
            return hit
    line = snippet.strip()
    if not line:
        return None
    if "def main(" in content and re.search(r"вывод|print|лог", user_text, re.I):
        hit = _insert_in_function(
            content,
            "main",
            line,
            (
                "    server.listen(5)",
                "    print(f'  Press Ctrl+C",
                '    print(f"  Press Ctrl+C',
                "    while True:",
            ),
        )
        if hit is not None:
            return hit
    if "__main__" in content and re.search(r"вывод|print", user_text, re.I):
        hit = insert_after_anchor(content, "if __name__", line)
        if hit is not None:
            return hit
    return None


@dataclass
class AppConfig:
    user_name: str
    api_key: str
    model: str = DEFAULT_MODEL
    version: int = CONFIG_VERSION

    def masked_api_key(self) -> str:
        k = self.api_key.strip()
        if len(k) <= 12:
            return "•" * len(k)
        return f"{k[:8]}…{k[-4:]}"


@dataclass
class FilePatch:
    name: str
    old_text: str
    new_text: str


def config_file_path() -> Path:
    return CONFIG_FILE


def load_config() -> AppConfig | None:
    path = config_file_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    api_key = str(data.get("api_key", "")).strip()
    user_name = str(data.get("user_name", "")).strip()
    if not api_key:
        return None
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        api_key = env_key
    return AppConfig(
        user_name=user_name or "Пользователь",
        api_key=api_key,
        model=str(data.get("model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL,
        version=int(data.get("version", CONFIG_VERSION)),
    )


def save_config(cfg: AppConfig) -> None:
    payload = {
        "version": CONFIG_VERSION,
        "user_name": cfg.user_name.strip(),
        "api_key": cfg.api_key.strip(),
        "model": (cfg.model or DEFAULT_MODEL).strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    config_file_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _setup_box_line(text: str, width: int = 54) -> str:
    inner = f"  {text}"
    pad = max(0, width - len(text) - 2)
    return f"{SALMON}│{RESET_STYLE}{SALMON_DIM}{inner}{' ' * pad}{RESET_STYLE}{SALMON}│{RESET_STYLE}"


def print_setup_frame(title: str, subtitle: str = "") -> None:
    w = 54
    print()
    print(f"{SALMON}╭{'─' * w}╮{RESET_STYLE}")
    print(_setup_box_line(title, w))
    if subtitle:
        print(_setup_box_line(subtitle, w))
    print(f"{SALMON}╰{'─' * w}╯{RESET_STYLE}")


def print_setup_step(step: int, total: int, title: str, hint: str = "") -> None:
    print()
    print(f"  {SALMON}●{RESET_STYLE} {SALMON_DIM}Шаг {step}/{total}{RESET_STYLE}  {SALMON}{title}{RESET_STYLE}")
    if hint:
        print(f"  {SALMON_DIM}{hint}{RESET_STYLE}")


def prompt_setup_value(label: str, *, secret: bool = False, default: str = "") -> str:
    suffix = f" {SALMON_DIM}(Enter — по умолчанию: {default}){RESET_STYLE}" if default else ""
    prompt = f"  {SALMON}{label}>{RESET_STYLE} "
    try:
        if secret:
            raw = getpass.getpass(prompt)
        else:
            raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise
    text = (raw or default).strip()
    return text


def run_setup_wizard(existing: AppConfig | None = None) -> AppConfig:
    os.system("") if sys.platform == "win32" else None
    print()
    for line in CORTEX_BANNER.strip().splitlines()[:4]:
        print(f"{SALMON}{line}{RESET_STYLE}")
    title = "Настройка профиля" if existing else "Добро пожаловать в Cortex Code"
    print(f"\n  {SALMON_DIM}Ключ OpenRouter: openrouter.ai/keys{RESET_STYLE}\n")

    total = 3
    default_name = existing.user_name if existing else ""
    default_model = existing.model if existing else DEFAULT_MODEL

    print_setup_step(1, total, "Ваше имя", "Отображается в приветствии")
    while True:
        name = prompt_setup_value("Имя", default=default_name)
        if name:
            break
        print(f"  {Fore.YELLOW}Введите имя (минимум 1 символ).{RESET_STYLE}")

    print_setup_step(2, total, "API-ключ ИИ", "Ввод не показывается !")
    while True:
        api_key = prompt_setup_value("API-ключ", secret=True)
        if not api_key and existing:
            api_key = existing.api_key
        if api_key.startswith("sk-") and len(api_key) >= 20:
            break
        print(f"  {Fore.YELLOW}Ключ должен начинаться с sk- и быть не короче 20 символов.{RESET_STYLE}")

    print_setup_step(
        3,
        total,
        "Модель",
        f"По умолчанию: {DEFAULT_MODEL}",
    )
    model = prompt_setup_value("Модель", default=default_model) or DEFAULT_MODEL

    cfg = AppConfig(user_name=name, api_key=api_key, model=model)
    save_config(cfg)

    print()
    print(f"{SALMON}╭{'─' * 54}╮{RESET_STYLE}")
    print(_setup_box_line("Готово — настройки сохранены", 54))
    print(_setup_box_line(f"Файл: {config_file_path().name}", 54))
    print(_setup_box_line(f"Пользователь: {cfg.user_name}", 54))
    print(_setup_box_line(f"Модель: {cfg.model}", 54))
    print(_setup_box_line(f"Ключ: {cfg.masked_api_key()}", 54))
    print(f"{SALMON}╰{'─' * 54}╯{RESET_STYLE}\n")
    return cfg


def ensure_config(*, force_setup: bool = False) -> AppConfig:
    if not force_setup:
        loaded = load_config()
        if loaded is not None:
            return loaded
        if config_file_path().is_file():
            print(f"{Fore.YELLOW}config.json повреждён — повторная настройка.{RESET_STYLE}")
    existing = load_config()
    return run_setup_wizard(existing)


def format_cursor_diff(patch: FilePatch, max_lines: int = DIFF_MAX_LINES) -> str:
    old_lines = patch.old_text.splitlines()
    new_lines = patch.new_text.splitlines()
    if old_lines == new_lines:
        return ""

    rows: list[tuple[str, str]] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            for line in old_lines[i1:i2]:
                rows.append(("del", line))
                removed += 1
        if tag in ("insert", "replace"):
            for line in new_lines[j1:j2]:
                rows.append(("add", line))
                added += 1

    if not rows:
        return ""

    truncated = False
    if len(rows) > max_lines:
        extra = len(rows) - max_lines
        rows = rows[:max_lines]
        truncated = True

    header = (
        f"{SALMON}{patch.name}{RESET_STYLE} "
        f"{Fore.GREEN}+{added}{RESET_STYLE} "
        f"{Fore.RED}-{removed}{RESET_STYLE}"
    )
    body: list[str] = [header]
    for kind, line in rows:
        if kind == "add":
            body.append(f"{Fore.GREEN}+{line}{RESET_STYLE}")
        else:
            body.append(f"{Fore.RED}-{line}{RESET_STYLE}")
    if truncated:
        body.append(f"{SALMON_DIM}  … ещё {extra} строк(и) не показано{RESET_STYLE}")
    return "\n".join(body)


def print_file_patches(patches: list[FilePatch]) -> None:
    if not patches:
        return
    print(f"\n{SALMON_DIM}── изменения ──{RESET_STYLE}")
    for patch in patches:
        block = format_cursor_diff(patch)
        if block:
            print(block)
    print()


@contextmanager
def thinking_bar(label: str = "Cortex") -> Iterator[None]:
    bar = ThinkingProgress(label)
    bar.start()
    try:
        yield
    finally:
        bar.stop(success=True)


class FileManager:
    def __init__(self, start_dir: Path | None = None) -> None:
        self.work_dir = (start_dir or Path.cwd()).resolve()
        self.pending_patches: list[FilePatch] = []

    def clear_patches(self) -> None:
        self.pending_patches = []

    def _commit_file(self, rel: str, new_content: str) -> tuple[bool, str]:
        path = self._resolve(rel)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            if old == new_content:
                return True, f"OK: без изменений — {path}"
            path.write_text(new_content, encoding="utf-8", newline="\n")
            self.pending_patches.append(
                FilePatch(name=Path(rel).name, old_text=old, new_text=new_content)
            )
            return True, f"OK: сохранено {path}"
        except OSError as exc:
            return False, f"Ошибка записи: {exc}"

    def _resolve(self, filename: str) -> Path:
        path = Path(filename.strip().strip('"\'`'))
        if path.is_absolute():
            return path.resolve()
        return (self.work_dir / path).resolve()

    def change_dir(self, path: str, create_if_missing: bool = False) -> str:
        target = Path(sanitize_windows_path(path)).expanduser()
        if not target.is_absolute():
            target = (self.work_dir / target).resolve()
        else:
            target = target.resolve()
        if not target.is_dir():
            if create_if_missing:
                target.mkdir(parents=True, exist_ok=True)
            else:
                return f"Ошибка: каталог не найден — {target}"
        self.work_dir = target
        return f"Рабочая директория: {self.work_dir}"

    def list_dir(self) -> str:
        lines = [f"Содержимое {self.work_dir}:", ""]
        try:
            entries = sorted(self.work_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return f"Ошибка: {exc}"
        if not entries:
            lines.append("  (пусто)")
        else:
            for e in entries:
                lines.append(f"  {'[DIR] ' if e.is_dir() else '      '}{e.name}")
        return "\n".join(lines)

    def read_file(self, filename: str) -> str:
        path = self._resolve(filename)
        if not path.is_file():
            return f"Ошибка: файл не найден — {path}"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Ошибка чтения: {exc}"

    def write_file(self, filename: str, content: str, create_only: bool = False) -> str:
        rel = safe_file_ref(filename, self.work_dir)
        if not rel:
            return f"Ошибка: недопустимое имя файла — {filename}"
        content = sanitize_file_content(content)
        path = self._resolve(rel)
        if create_only and path.exists():
            return f"Ошибка: файл уже существует — {path}"
        ok, msg = self._commit_file(rel, content)
        if ok and "сохранено" in msg:
            return f"{msg} ({len(content)} символов)"
        return msg

    def apply_search_replace(self, filename: str, search: str, replace: str) -> str:
        rel = safe_file_ref(filename, self.work_dir)
        if not rel:
            return f"Ошибка: недопустимое имя файла — {filename}"
        path = self._resolve(rel)
        if not path.is_file():
            return f"Ошибка: файл не найден — {path}"
        search = sanitize_file_content(search)
        replace = sanitize_file_content(replace)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Ошибка чтения: {exc}"
        if not search.strip():
            new_content = replace + ("\n" + content if content else "")
            action = "добавлено в начало"
        elif not replace.strip():
            if search not in content:
                return f"Ошибка: фрагмент для удаления не найден в {path}"
            new_content = content.replace(search, "", 1)
            action = "удалён фрагмент"
        else:
            if search not in content:
                return f"Ошибка: точное совпадение не найдено в {path}"
            new_content = content.replace(search, replace, 1)
            action = "заменено"
        ok, msg = self._commit_file(rel, new_content)
        if ok:
            return f"{msg} ({action})"
        return msg

    def replace_in_file(self, filename: str, search: str, replace: str) -> str:
        return self.apply_search_replace(filename, search, replace)

    def apply_replace_body(self, filename: str, body: str, user_text: str = "") -> str:
        rel = safe_file_ref(filename, self.work_dir)
        if not rel:
            return f"Ошибка: недопустимое имя файла — {filename}"
        parts = parse_replace_parts(body)
        if parts is not None:
            return self.apply_search_replace(rel, parts[0], parts[1])
        snippet = sanitize_file_content(body)
        if not snippet.strip():
            return f"Ошибка REPLACE {rel}: пустое тело"
        path = self._resolve(rel)
        if not path.is_file():
            return f"Ошибка: файл не найден — {path}"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Ошибка чтения: {exc}"
        if snippet in content:
            return f"OK: без изменений — фрагмент уже есть в {path}"
        inserted = smart_insert_snippet(content, snippet, user_text)
        if inserted is not None:
            ok, msg = self._commit_file(rel, inserted)
            if ok:
                return f"{msg} (REPLACE без ---, по контексту запроса)"
            return msg
        append_msg = self.append_to_file(rel, snippet)
        if append_msg.startswith("OK:"):
            return append_msg.replace(
                "OK: дополнено",
                "OK: REPLACE без --- → дописано в конец",
                1,
            )
        return append_msg

    def append_to_file(self, filename: str, content: str) -> str:
        rel = safe_file_ref(filename, self.work_dir)
        if not rel:
            return f"Ошибка: недопустимое имя файла — {filename}"
        content = sanitize_file_content(content)
        path = self._resolve(rel)
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing and not existing.endswith("\n") and content and not content.startswith("\n"):
            existing += "\n"
        ok, msg = self._commit_file(rel, existing + content)
        if ok:
            return f"{msg} (+{len(content)} символов)"
        return msg

    def delete_file(self, filename: str) -> str:
        path = self._resolve(filename)
        if not path.exists():
            return f"Ошибка: не найдено — {path}"
        try:
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
            return f"OK: удалено — {path}"
        except OSError as exc:
            return f"Ошибка удаления: {exc}"

    def run_python(self, filename: str) -> str:
        path = self._resolve(filename)
        if not path.is_file():
            return f"Ошибка: файл не найден — {path}"
        try:
            result = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(self.work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Ошибка: таймаут 120 с."
        except OSError as exc:
            return f"Ошибка запуска: {exc}"
        out = []
        if result.stdout:
            out.append(result.stdout.rstrip())
        if result.stderr:
            out.append("--- stderr ---\n" + result.stderr.rstrip())
        body = "\n".join(out) if out else "(нет вывода)"
        return f"--- {path.name} (код {result.returncode}) ---\n{body}"

    def _save_from_match(self, path: str, content: str, logs: list[str]) -> None:
        rel = safe_file_ref(path, self.work_dir)
        if not rel:
            logs.append(f"Пропущен файл (плохой путь): {path}")
            return
        logs.append(self.write_file(rel, content, create_only=False))

    def apply_ai_file_blocks(self, text: str) -> tuple[str, list[str]]:
        logs: list[str] = []

        def repl(m: re.Match[str]) -> str:
            self._save_from_match(m.group("path"), m.group("content"), logs)
            return ""

        out = FILE_BLOCK_PATTERN.sub(repl, text)
        out = FILE_BLOCK_LOOSE_PATTERN.sub(repl, out)
        return out.strip(), logs

    def apply_ai_replace_blocks(
        self, text: str, user_text: str = ""
    ) -> tuple[str, list[str]]:
        logs: list[str] = []

        def repl(m: re.Match[str]) -> str:
            rel = safe_file_ref(m.group("path"), self.work_dir)
            if not rel:
                logs.append(f"Пропущен REPLACE (плохой путь): {m.group('path')}")
                return ""
            logs.append(self.apply_replace_body(rel, m.group("body"), user_text))
            return ""

        return REPLACE_BLOCK_PATTERN.sub(repl, text).strip(), logs

    def apply_ai_append_blocks(self, text: str) -> tuple[str, list[str]]:
        logs: list[str] = []

        def repl(m: re.Match[str]) -> str:
            rel = safe_file_ref(m.group("path"), self.work_dir)
            if not rel:
                logs.append(f"Пропущен APPEND (плохой путь): {m.group('path')}")
                return ""
            logs.append(self.append_to_file(rel, m.group("content")))
            return ""

        return APPEND_BLOCK_PATTERN.sub(repl, text).strip(), logs

    def apply_ai_cd_blocks(self, text: str) -> tuple[str, list[str]]:

        def repl(m: re.Match[str]) -> str:
            return ""

        return CD_BLOCK_PATTERN.sub(repl, text).strip(), []

    def apply_longcat_tool_calls(self, text: str) -> tuple[str, list[str]]:
        logs: list[str] = []

        def repl(m: re.Match[str]) -> str:
            self._save_from_match(m.group("path"), m.group("content"), logs)
            return ""

        return LONGCAT_FILE_PATTERN.sub(repl, text).strip(), logs

    def apply_code_fences(self, text: str, filename: str) -> tuple[str, list[str]]:
        logs: list[str] = []
        blocks = CODE_FENCE_PATTERN.findall(text)
        for i, code in enumerate(blocks):
            name = filename if len(blocks) == 1 else f"{Path(filename).stem}_{i + 1}{Path(filename).suffix}"
            if is_safe_filename(name):
                logs.append(self.write_file(name, code, False))
        return CODE_FENCE_PATTERN.sub("", text).strip(), logs

    def apply_ai_artifacts(
        self,
        text: str,
        fallback_filename: str,
        *,
        partial_edit_only: bool = False,
        user_text: str = "",
    ) -> tuple[str, list[str]]:
        self.clear_patches()
        logs: list[str] = []
        visible, part = self.apply_longcat_tool_calls(text)
        logs.extend(part)
        visible, part = self.apply_ai_replace_blocks(visible, user_text)
        logs.extend(part)
        visible, part = self.apply_ai_append_blocks(visible)
        logs.extend(part)
        if partial_edit_only:
            if response_has_full_file_block(visible):
                logs.append(
                    f"Пропущен <<<FILE:{fallback_filename}>>> — режим умной правки "
                    f"(ожидаются REPLACE/APPEND, бюджет {PARTIAL_EDIT_TARGET_OUTPUT} tok)"
                )
        else:
            visible, part = self.apply_ai_file_blocks(visible)
            logs.extend(part)
        visible, part = self.apply_ai_cd_blocks(visible)
        logs.extend(part)
        patch_ok = any("OK:" in x for x in logs)
        if not patch_ok and not partial_edit_only and is_safe_filename(fallback_filename):
            visible, part = self.apply_code_fences(visible, fallback_filename)
            logs.extend(part)
        return visible, logs


class SessionContext:
    def __init__(self) -> None:
        self.pending_dir: str | None = None
        self.last_request: str = ""
        self.last_file: str | None = None

    def remember_request(self, text: str, path: str | None) -> None:
        self.last_request = text
        if path:
            self.pending_dir = path
        m = re.search(r"([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)", text)
        if m:
            self.last_file = Path(m.group(1)).name

    def remember_file(self, filename: str) -> None:
        self.last_file = Path(filename).name

    def is_continuation(self, text: str) -> bool:
        return bool(CONTINUATION_PATTERN.match(text.strip()))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


class ChatStore:

    def __init__(self) -> None:
        CHATS_DIR.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Any] = {"active_id": None, "chats": {}}
        self._load_index()
        self._migrate_legacy_history()
        if not self._index["chats"]:
            self.new_chat("Основной")
        elif not self._index.get("active_id"):
            self._index["active_id"] = next(iter(self._index["chats"]))

    def _load_index(self) -> None:
        if CHATS_INDEX_FILE.exists():
            try:
                self._index = json.loads(CHATS_INDEX_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        self._index.setdefault("active_id", None)
        self._index.setdefault("chats", {})

    def _save_index(self) -> None:
        CHATS_INDEX_FILE.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _chat_path(self, chat_id: str) -> Path:
        return CHATS_DIR / f"{chat_id}.json"

    def _migrate_legacy_history(self) -> None:
        if not HISTORY_FILE.exists() or self._index["chats"]:
            return
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, list) or not data:
            return
        cid = self._new_id()
        self._index["chats"][cid] = {
            "id": cid,
            "name": "Импорт",
            "created": time.time(),
        }
        self._index["active_id"] = cid
        self._save_messages(cid, data)
        self._save_index()
        try:
            HISTORY_FILE.rename(HISTORY_FILE.with_suffix(".json.bak"))
        except OSError:
            pass

    @staticmethod
    def _new_id() -> str:
        return f"chat_{int(time.time() * 1000)}"

    def active_id(self) -> str:
        aid = self._index.get("active_id")
        if aid and aid in self._index["chats"]:
            return aid
        if self._index["chats"]:
            aid = next(iter(self._index["chats"]))
            self._index["active_id"] = aid
            self._save_index()
            return aid
        return self.new_chat("Основной")

    def active_name(self) -> str:
        return self._index["chats"].get(self.active_id(), {}).get("name", "?")

    def list_chats(self) -> list[tuple[str, str, bool]]:
        aid = self.active_id()
        out: list[tuple[str, str, bool]] = []
        for cid, meta in sorted(
            self._index["chats"].items(),
            key=lambda x: x[1].get("created", 0),
        ):
            out.append((cid, meta.get("name", cid), cid == aid))
        return out

    def new_chat(self, name: str | None = None) -> str:
        cid = self._new_id()
        label = (name or f"Чат {len(self._index['chats']) + 1}").strip()[:40]
        self._index["chats"][cid] = {"id": cid, "name": label, "created": time.time()}
        self._index["active_id"] = cid
        self._save_messages(cid, [])
        self._save_index()
        return cid

    def switch_chat(self, target: str) -> str:
        target = target.strip()
        if target in self._index["chats"]:
            self._index["active_id"] = target
            self._save_index()
            return f"Чат: {self.active_name()}"
        for cid, meta in self._index["chats"].items():
            if meta.get("name", "").lower() == target.lower():
                self._index["active_id"] = cid
                self._save_index()
                return f"Чат: {self.active_name()}"
        if target.isdigit():
            chats = self.list_chats()
            idx = int(target) - 1
            if 0 <= idx < len(chats):
                self._index["active_id"] = chats[idx][0]
                self._save_index()
                return f"Чат: {self.active_name()}"
        return f"Чат не найден: {target}"

    def rename_active(self, name: str) -> str:
        cid = self.active_id()
        self._index["chats"][cid]["name"] = name.strip()[:40]
        self._save_index()
        return f"Переименовано в: {self.active_name()}"

    def delete_chat(self, target: str) -> str:
        cid = None
        if target in self._index["chats"]:
            cid = target
        else:
            for c, meta in self._index["chats"].items():
                if meta.get("name", "").lower() == target.lower():
                    cid = c
                    break
        if not cid:
            return f"Чат не найден: {target}"
        if len(self._index["chats"]) <= 1:
            return "Нельзя удалить единственный чат. Создайте другой: /newchat"
        path = self._chat_path(cid)
        if path.exists():
            path.unlink()
        del self._index["chats"][cid]
        if self._index["active_id"] == cid:
            self._index["active_id"] = next(iter(self._index["chats"]))
        self._save_index()
        return f"Удалён чат. Активен: {self.active_name()}"

    def _save_messages(self, chat_id: str, messages: list[dict[str, str]]) -> None:
        self._chat_path(chat_id).write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_messages(self, chat_id: str | None = None) -> list[dict[str, str]]:
        cid = chat_id or self.active_id()
        path = self._chat_path(cid)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def save_active_messages(self, messages: list[dict[str, str]]) -> None:
        to_save = [m for m in messages if m["role"] != "system"]
        self._save_messages(self.active_id(), to_save)


def detect_files_for_context(text: str, session: SessionContext, fm: FileManager) -> list[str]:
    names: list[str] = []
    for m in re.finditer(r"([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)", text):
        names.append(m.group(1))
    if session.last_file and session.last_file not in names:
        if EDIT_REQUEST_PATTERN.search(text):
            names.append(session.last_file)
    result: list[str] = []
    for name in names:
        if fm._resolve(name).is_file():
            result.append(name)
    return result


def build_user_payload(
    user_text: str,
    fm: FileManager,
    session: SessionContext,
) -> tuple[str, list[str]]:
    parts = [user_text]
    prepared: list[str] = []
    files = collect_context_files(user_text, fm, session)
    target = extract_target_filename(user_text)

    for fname in files:
        content = fm.read_file(fname)
        if content.startswith("Ошибка"):
            parts.append(f"\n\n[Файл {fname} не найден в рабочей папке]")
            continue
        prepared.append(fname)
        tag = _lang_tag_for_file(fname)
        parts.append(
            f"\n\n═══ ИСТОЧНИК: {fname} (уже прочитан Cortex, используй содержимое) ═══\n"
            f"```{tag}\n{content}\n```"
        )

    if target and wants_any_file(user_text):
        if prefers_partial_edit(user_text, target, fm):
            parts.append(
                f"\n\n[Задача: минимальная правка {target} — <<<REPLACE:{target}>>> или "
                f"<<<APPEND:{target}>>>, не переписывай весь файл. Сделай в этом ответе.]"
            )
        else:
            parts.append(
                f"\n\n[Задача: создай/обнови <<<FILE:{target}>>> на основе запроса и источников выше. "
                f"Не откладывай — сделай в этом ответе.]"
            )

    return "\n".join(parts), prepared


def split_leading_path_command(text: str) -> tuple[str | None, str]:
    m = LEADING_PATH_COMMAND.match(text.strip())
    if not m:
        return None, text
    path = sanitize_windows_path(m.group("path"))
    if not is_real_windows_path(path):
        return None, text
    rest = m.group("rest").strip()
    return path, rest if rest else text


def sanitize_windows_path(raw: str) -> str:
    raw = raw.strip().strip('"\'`').rstrip(".,!?;:")
    m = re.match(rf"^([A-Za-z]:\\(?:{_WIN_SEG}\\)*{_WIN_SEG})", raw)
    if m:
        return m.group(1)
    for sep in (" в ", " во ", " на "):
        pos = raw.lower().find(sep)
        if pos > 0:
            head = raw[:pos].strip()
            if re.match(r"^[A-Za-z]:\\", head):
                return sanitize_windows_path(head)
    return raw


def extract_directory_from_message(text: str) -> str | None:
    leading, _ = split_leading_path_command(text)
    if leading:
        return leading
    for pattern in DIR_REQUEST_PATTERNS:
        m = pattern.search(text)
        if m:
            p = sanitize_windows_path(m.group("path"))
            if len(p) >= 3:
                return p
    if re.search(r"сделай|создай|тут|код|директор|папк", text, re.I):
        paths = [sanitize_windows_path(m.group("path")) for m in WIN_PATH_PATTERN.finditer(text)]
        if paths:
            return paths[0]
    return None


def guess_filename_from_request(text: str) -> str:
    fn = extract_target_filename(text)
    if fn and is_safe_filename(fn):
        return fn
    t = text.lower()
    if "html" in t or "сайт" in t:
        return "index.html"
    if "gaid" in t or "guide" in t or "гайд" in t:
        return "gaid.html"
    if "cortex" in t and "info" in t:
        return "cortexinfo.txt"
    if "check" in t:
        return "check.py"
    if "парол" in t and ("генератор" in t or "generator" in t):
        return "password_generator.py"
    return "main.py"


def default_start_directory() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if str(cwd).lower().startswith(r"c:\windows"):
        return APP_DIR
    for unsafe in UNSAFE_START_DIRS:
        try:
            if cwd == unsafe.resolve():
                return APP_DIR
        except OSError:
            pass
    return cwd


def try_change_dir_from_user_message(text: str, fm: FileManager, session: SessionContext) -> str | None:
    path = extract_directory_from_message(text)
    if not path and session.is_continuation(text) and session.pending_dir:
        path = session.pending_dir
    if not path:
        return None
    session.remember_request(text, path)
    create = bool(
        re.search(r"созда|сделай|тут|здесь|код", text, re.I)
        or session.is_continuation(text)
        or wants_any_file(text)
    )
    return fm.change_dir(path, create_if_missing=create)


class AIClient:
    def __init__(self, fm: FileManager, chat_store: ChatStore, config: AppConfig) -> None:
        self.file_manager = fm
        self.chat_store = chat_store
        self.config = config
        self.reload_active_chat()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "HTTP-Referer": HTTP_REFERER,
            "X-Title": APP_TITLE,
            "Content-Type": "application/json",
        }

    def _system_content(self) -> str:
        return build_system_prompt(self.file_manager.work_dir)

    def refresh_system_prompt(self) -> None:
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = self._system_content()
        else:
            self.messages.insert(0, {"role": "system", "content": self._system_content()})

    def reload_active_chat(self) -> None:
        self.messages = [{"role": "system", "content": self._system_content()}]
        for item in self.chat_store.load_messages():
            if item.get("role") in ("user", "assistant") and item.get("content"):
                self.messages.append({"role": item["role"], "content": str(item["content"])})

    def context_stats(self, extra_user_text: str = "") -> tuple[int, int, float]:
        used = count_messages_tokens(self.messages) + estimate_tokens(extra_user_text)
        pct = min(100.0, used / MAX_CONTEXT_TOKENS * 100)
        return used, MAX_CONTEXT_TOKENS, pct

    def context_full(self, extra_user_text: str = "") -> bool:
        used, _, _ = self.context_stats(extra_user_text)
        return used >= MAX_CONTEXT_TOKENS

    def format_context_bar(self, extra_user_text: str = "") -> str:
        used, max_t, pct = self.context_stats(extra_user_text)
        color = SALMON
        if pct >= 100:
            color = Fore.RED
        elif pct >= 85:
            color = Fore.YELLOW
        return f"{color}ctx {pct:4.1f}%{RESET_STYLE} {SALMON_DIM}({used // 1000}k/{max_t // 1000}k){RESET_STYLE}"

    def save_history(self) -> None:
        self.chat_store.save_active_messages(self.messages)

    def clear_history(self) -> None:
        self.messages = [{"role": "system", "content": self._system_content()}]
        self.chat_store.save_active_messages([])

    def _call_model(
        self,
        pipeline: CortexPipeline,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, str]] | None = None,
    ) -> tuple[str | None, str | None, dict[str, int]]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages if messages is not None else self.messages,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        try:
            with pipeline.wait_api("модель формирует ответ…"):
                resp = requests.post(
                    OPENROUTER_URL,
                    headers=self._headers(),
                    json=payload,
                    timeout=120,
                )
                resp.raise_for_status()
                data = resp.json()
        except requests.exceptions.Timeout:
            return None, "Ошибка: таймаут API.", {}
        except requests.exceptions.HTTPError as exc:
            detail = exc.response.text[:400] if exc.response is not None else ""
            return None, f"Ошибка HTTP: {detail}", {}
        except requests.exceptions.RequestException as exc:
            return None, f"Ошибка сети: {exc}", {}

        usage: dict[str, int] = {}
        raw_usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(raw_usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if key in raw_usage:
                    usage[key] = int(raw_usage[key])

        try:
            reply = data["choices"][0]["message"]["content"] or "(пустой ответ)"
        except (KeyError, IndexError, TypeError):
            return None, f"Неожиданный ответ: {data}", usage
        return reply, None, usage

    def _process_reply(
        self,
        reply: str,
        fn: str,
        needs_file: bool,
        session: SessionContext | None,
        user_text: str = "",
    ) -> tuple[str, list[str], bool]:
        raw = LONGCAT_READ_STUB.sub("", reply)
        raw = READ_PROMISE_STUB.sub("", raw)
        partial = prefers_partial_edit(user_text, fn, self.file_manager)
        _, logs = self.file_manager.apply_ai_artifacts(
            raw, fn, partial_edit_only=partial, user_text=user_text
        )
        file_saved = any("OK:" in x for x in logs)
        for log in logs:
            if "OK:" in log:
                m = re.search(r"([^\s\\]+\.[a-z0-9]+)", log, re.I)
                if m and session:
                    session.remember_file(m.group(1))
        visible = clean_ai_visible(raw)
        visible = finalize_visible_reply(
            visible, fn, needs_file, file_saved, partial_edit=partial
        )
        if logs:
            report = "\n".join(logs)
            visible = f"{visible}\n\n{report}".strip() if visible.strip() else report
        return visible, logs, file_saved

    def chat(
        self, user_text: str, session: SessionContext | None = None
    ) -> tuple[str, list[str]]:
        pipeline = CortexPipeline()
        ctx = user_text
        if session and session.last_request and session.is_continuation(user_text):
            ctx = f"{session.last_request}\n{user_text}"

        pipeline.stage(1, "Анализ запроса", "определяю задачу и целевой файл")
        needs_file = bool(wants_any_file(ctx) or EDIT_REQUEST_PATTERN.search(ctx))
        fn = guess_filename_from_request(ctx)
        if session and session.last_file and fn == "main.py":
            fn = session.last_file
        if needs_file:
            pipeline.thought(f"нужен файл: {fn}")
        partial_edit = prefers_partial_edit(ctx, fn, self.file_manager)
        if partial_edit:
            pipeline.thought(
                f"умная правка {fn} (лимит {PARTIAL_EDIT_API_MAX_TOKENS} tok выхода)"
            )
        elif needs_file and self.file_manager._resolve(fn).is_file():
            pipeline.thought(f"файл {fn} есть, но режим полного FILE (нет глагола правки?)")

        pipeline.stage(2, "Сбор контекста", "читаю источники для промпта")
        payload_text, prepared_files = build_user_payload(
            user_text, self.file_manager, session or SessionContext()
        )
        if prepared_files:
            pipeline.thought(f"вложено в запрос: {', '.join(prepared_files)}")
        else:
            pipeline.thought("дополнительные файлы не требуются")

        self.refresh_system_prompt()
        wd = str(self.file_manager.work_dir.resolve())

        extra = (
            f"\n\n[WORK_DIR={wd}]\n"
            f"[Задача: все файлы сохраняются только в WORK_DIR. "
            f"Минимальные правки — не переписывай файл целиком без необходимости.]"
        )
        if needs_file and partial_edit:
            extra += (
                f"\n[РЕЖИМ УМНОЙ ПРАВКИ {fn}: <<<REPLACE:{fn}>>> старый_код\\n---\\nновый_код <<<END>>> "
                f"или <<<APPEND:{fn}>>> новые_строки <<<END>>>. "
                f"Для «добавь print» — предпочитай APPEND. Бюджет {PARTIAL_EDIT_TARGET_OUTPUT} tok. "
                f"<<<FILE:{fn}>>> ЗАПРЕЩЁН. Без гайдов — только блок и 1 строка итога.]"
            )
        elif needs_file:
            extra += (
                f"\n[ОБЯЗАТЕЛЬНО: <<<FILE:{fn}>>> с полным содержимым (файла нет или переписать). "
                f"ЗАПРЕЩЕНО «создаю», «сейчас сделаю», «подожди» — только файл и итог.]"
            )
        if prepared_files:
            if partial_edit:
                extra += (
                    f"\n[Источники уже в сообщении: {', '.join(prepared_files)}. "
                    f"Правь через REPLACE/APPEND по фрагменту из источника, не переписывай весь файл.]"
                )
            else:
                extra += (
                    f"\n[Источники уже в сообщении: {', '.join(prepared_files)}. "
                    f"Сразу выдай <<<FILE:{fn}>>>, не обещай читать или создавать позже.]"
                )

        user_msg = f"{payload_text}{extra}"
        api_max = PARTIAL_EDIT_API_MAX_TOKENS if partial_edit else None

        if partial_edit:
            api_messages: list[dict[str, str]] = build_lite_edit_messages(
                ctx, fn, self.file_manager, prepared_files, extra
            )
            lite_tok = count_messages_tokens(api_messages)
            hist_tok = count_messages_tokens(self.messages)
            if lite_tok >= MAX_CONTEXT_TOKENS:
                return (
                    f"Файл слишком большой для lite-правки (~{lite_tok:,} tok). "
                    f"Укоротите файл или /newchat.",
                    prepared_files,
                )
            pipeline.thought(
                f"lite-запрос ~{lite_tok // 1000}k tok "
                f"(история чата ~{hist_tok // 1000}k tok не уходит в API)"
            )
        else:
            api_messages = self.messages + [{"role": "user", "content": user_msg}]
            if self.context_full(user_msg):
                used, max_t, pct = self.context_stats(user_msg)
                return (
                    f"Контекст переполнен ({pct:.0f}% — {used:,} / {max_t:,} токенов).\n"
                    "Новый чат: /newchat  |  Очистить этот: /clear",
                    [],
                )

        cap_note = f" · лимит выхода {api_max} tok" if api_max else ""
        mode_note = " · lite (без истории)" if partial_edit else ""
        pipeline.stage(
            3, "Запрос к модели", f"{self.config.model} · {wd}{mode_note}{cap_note}"
        )
        reply, err, usage = self._call_model(
            pipeline, max_tokens=api_max, messages=api_messages
        )
        if err:
            return err, prepared_files

        mode = classify_reply_mode(reply, partial_edit)
        if usage.get("prompt_tokens"):
            mode += f" · API in {usage['prompt_tokens']} tok"
        if usage.get("completion_tokens"):
            mode += f" · API out {usage['completion_tokens']} tok"
        elif usage.get("total_tokens"):
            mode += f" · API total {usage['total_tokens']} tok"
        pipeline.thought(f"режим ответа: {mode}")

        pipeline.stage(4, "Разбор ответа", "REPLACE/APPEND/FILE → диск")
        visible, logs, file_saved = self._process_reply(reply, fn, needs_file, session, ctx)

        if should_retry_bloated_partial_edit(reply, partial_edit, file_saved):
            est = reply_output_estimate(reply)
            pipeline.thought(
                f"раздутый ответ (~{est} tok) без REPLACE/APPEND — повтор с жёстким лимитом"
            )
            bloated_note = (
                f"\n\n[СИСТЕМА Cortex: ответ ~{est} токенов — это НЕ умная правка. "
                f"Нужен ТОЛЬКО один блок:\n<<<REPLACE:{fn}>>>\n<3–15 строк из файла> --- <исправленные строки>\n<<<END>>>\n"
                f"или <<<APPEND:{fn}>>>\n<новый код>\n<<<END>>>\n"
                f"Без гайдов, без <<<FILE:{fn}>>>, без markdown. Видимый текст ≤ 2 предложений.]"
            )
            retry_msgs = api_messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": bloated_note},
            ]
            pipeline.stage(3, "Повтор (умная правка)", f"лимит {PARTIAL_EDIT_API_MAX_TOKENS} tok")
            reply_b, err_b, _ = self._call_model(
                pipeline,
                max_tokens=PARTIAL_EDIT_API_MAX_TOKENS,
                messages=retry_msgs,
            )
            if not err_b:
                visible_b, logs_b, saved_b = self._process_reply(
                    reply_b, fn, needs_file, session, ctx
                )
                mode_b = classify_reply_mode(reply_b, partial_edit)
                pipeline.thought(f"повтор: {mode_b}")
                if saved_b or reply_output_estimate(reply_b) < est:
                    reply = reply_b
                    visible, logs, file_saved = visible_b, logs_b, saved_b

        if needs_file and not file_saved:
            block_hint = (
                f"<<<REPLACE:{fn}>>>\n<фрагмент> --- <новый фрагмент>\n<<<END>>>"
                if partial_edit
                else f"<<<FILE:{fn}>>>\n<полное содержимое файла>\n<<<END>>>"
            )
            pipeline.thought(f"файл {fn} не записан — повторный запрос")
            retry_note = (
                f"\n\n[СИСТЕМА Cortex: предыдущий ответ НЕ изменил {fn}. "
                f"Ответь ТОЛЬКО одним блоком:\n{block_hint}\n"
                f"Без вступлений, без «создаю» и без markdown-ограждений.]"
            )
            retry_msgs = api_messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": retry_note},
            ]
            pipeline.stage(3, "Повтор запроса", block_hint.split("\n", 1)[0][:48])
            reply2, err2, _ = self._call_model(
                pipeline,
                max_tokens=PARTIAL_EDIT_API_MAX_TOKENS if partial_edit else None,
                messages=retry_msgs,
            )
            if err2:
                return err2, prepared_files
            visible2, logs2, saved2 = self._process_reply(reply2, fn, needs_file, session, ctx)
            if saved2:
                reply = reply2
                visible, logs, file_saved = visible2, logs2, saved2
            else:
                pipeline.thought("повтор не записал файл на диск")
                visible = visible2
                logs = logs2

        pipeline.stage(5, "Готово", "файл записан" if file_saved else "ответ без нового файла")
        if file_saved:
            pipeline.done(f"сохранено в {wd}")
        else:
            pipeline.done("ответ получен")

        if partial_edit:
            self.messages.append(
                {"role": "user", "content": f"[правка {fn}] {user_text.strip()}"}
            )
            self.messages.append({"role": "assistant", "content": visible})
        else:
            self.messages.append({"role": "user", "content": user_msg})
            self.messages.append({"role": "assistant", "content": reply})
        self.save_history()
        return visible, prepared_files




CORTEX_BANNER = r"""
 ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗
██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝
██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝
██║     ██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗
╚██████╗╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗
 ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
"""

CODE_BANNER = r"""
 ██████╗ ██████╗ ██████╗ ███████╗
██╔════╝██╔═══██╗██╔══██╗██╔════╝
██║     ██║   ██║██║  ██║█████╗
██║     ██║   ██║██║  ██║██╔══╝
╚██████╗╚██████╔╝██████╔╝███████╗
 ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""


def print_banner(config: AppConfig) -> None:
    hello = f" * Привет, {config.user_name}! Cortex Code * "
    print(f"\n{SALMON}╭{'─' * (len(hello) + 2)}╮{RESET_STYLE}")
    print(f"{SALMON}│{hello}│{RESET_STYLE}")
    print(f"{SALMON}╰{'─' * (len(hello) + 2)}╯{RESET_STYLE}\n")
    for block in (CORTEX_BANNER.strip(), CODE_BANNER.strip()):
        for line in block.splitlines():
            print(f"{SALMON}{line}{RESET_STYLE}")
        print()
    print(f"{SALMON_DIM}OpenRouter · {config.model}{RESET_STYLE}")
    print(f"{SALMON_DIM}Ключ: {config.masked_api_key()} · /config — сменить{RESET_STYLE}")
    print(f"{SALMON_DIM}Контекст: до {MAX_CONTEXT_TOKENS // 1000}k токенов{RESET_STYLE}\n")


def print_help() -> None:
    print(f"""{SALMON_DIM}Папка:  /directory [путь] — сменить (без аргумента — ввод вручную)
         /cd [путь] — то же | показать: /directory
Файлы:  /create /edit /read /delete /run /dir
Чаты:   /chats | /newchat [имя] | /chat [имя|№] | /renamechat | /delchat | /clear
Настройки: /config — имя, API-ключ, модель (config.json)
Контекст: макс. {MAX_CONTEXT_TOKENS // 1000}k; при 100% — /newchat или /clear
Многострочный ввод: {MULTILINE_END_TRIPLE} или {MULTILINE_END_FENCE}{RESET_STYLE}""")


def print_chats_list(store: ChatStore) -> None:
    print(f"{SALMON}Чаты:{RESET_STYLE}")
    for i, (cid, name, active) in enumerate(store.list_chats(), 1):
        mark = f"{SALMON}*{RESET_STYLE}" if active else " "
        short = cid[-8:]
        print(f"  {mark} {i}. {name} ({short})")
    print(f"{SALMON_DIM}Переключить: /chat 2  или  /chat Имя{RESET_STYLE}")


def read_multiline(end_marker: str) -> str:
    lines = []
    print(f"{SALMON_DIM}(завершите строкой {end_marker}){RESET_STYLE}")
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == end_marker:
            break
        lines.append(line)
    return "\n".join(lines)


def read_user_input(prompt: str) -> str | None:
    try:
        first = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    if first.strip() in (MULTILINE_END_TRIPLE, MULTILINE_END_FENCE):
        return read_multiline(first.strip())
    return first


def parse_command(line: str) -> tuple[str, str]:
    if not line.startswith("/"):
        return "", line
    parts = line[1:].split(maxsplit=1)
    return (parts[0].lower(), parts[1]) if parts else ("", "")


def cmd_set_directory(arg: str, fm: FileManager, ai: AIClient) -> None:
    if arg.strip():
        print(f"{SALMON}{fm.change_dir(arg.strip(), create_if_missing=True)}{RESET_STYLE}")
    else:
        print(f"{SALMON_DIM}Текущая папка: {fm.work_dir}{RESET_STYLE}")
        new_path = input(f"{SALMON}Новая директория> {RESET_STYLE}").strip()
        if new_path:
            print(f"{SALMON}{fm.change_dir(new_path, create_if_missing=True)}{RESET_STYLE}")
    ai.refresh_system_prompt()


def handle_slash_command(
    cmd: str,
    arg: str,
    fm: FileManager,
    ai: AIClient,
    store: ChatStore,
    config: AppConfig,
) -> bool:
    if cmd in ("exit", "quit", "q"):
        print(f"{SALMON}До свидания!{RESET_STYLE}")
        return False
    if cmd == "help":
        print_help()
        return True
    if cmd in ("config", "settings", "setup"):
        new_cfg = run_setup_wizard(config)
        config.user_name = new_cfg.user_name
        config.api_key = new_cfg.api_key
        config.model = new_cfg.model
        print(f"{Fore.GREEN}Настройки обновлены.{Style.RESET_ALL}")
        return True
    if cmd in ("chats", "chatlist"):
        print_chats_list(store)
        return True
    if cmd in ("newchat", "new"):
        name = arg.strip() or None
        store.new_chat(name)
        ai.reload_active_chat()
        print(f"{SALMON}Новый чат: {store.active_name()}{RESET_STYLE}")
        return True
    if cmd == "chat" and not arg:
        print_chats_list(store)
        return True
    if cmd == "chat" and arg:
        print(f"{SALMON}{store.switch_chat(arg)}{RESET_STYLE}")
        ai.reload_active_chat()
        return True
    if cmd == "renamechat" and arg:
        print(f"{SALMON}{store.rename_active(arg)}{RESET_STYLE}")
        return True
    if cmd in ("delchat", "deletechat") and arg:
        print(f"{SALMON}{store.delete_chat(arg)}{RESET_STYLE}")
        ai.reload_active_chat()
        return True
    if cmd in ("directory", "workdir", "pwd"):
        cmd_set_directory(arg, fm, ai)
        return True
    if cmd == "dir":
        print(fm.list_dir())
        return True
    if cmd == "cd":
        cmd_set_directory(arg, fm, ai)
        return True
    if cmd == "read" and arg:
        print(fm.read_file(arg))
        return True
    if cmd == "delete" and arg:
        print(fm.delete_file(arg))
        return True
    if cmd == "run" and arg:
        print(fm.run_python(arg))
        return True
    if cmd == "clear":
        ai.clear_history()
        print(f"{Fore.GREEN}Чат «{store.active_name()}» очищен.{Style.RESET_ALL}")
        return True
    if cmd == "create" and arg:
        print(f"{SALMON_DIM}Введите содержимое ({MULTILINE_END_TRIPLE} или {MULTILINE_END_FENCE} — многострочно):{RESET_STYLE}")
        content = read_user_input("> ")
        if content is not None:
            fm.clear_patches()
            print(fm.write_file(arg, content, create_only=True))
            if fm.pending_patches:
                print_file_patches(fm.pending_patches)
                fm.clear_patches()
        return True
    if cmd == "edit" and arg:
        old = fm.read_file(arg)
        if not old.startswith("Ошибка"):
            print(old)
        content = read_user_input("> ")
        if content is not None:
            fm.clear_patches()
            print(fm.write_file(arg, content, False))
            if fm.pending_patches:
                print_file_patches(fm.pending_patches)
                fm.clear_patches()
        return True
    if cmd:
        print(f"{Fore.YELLOW}Неизвестная команда /{cmd}{Style.RESET_ALL}")
    return True


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
        os.system("")

    colorama_init(autoreset=True)

    if not config_file_path().is_file():
        pass
    try:
        app_config = ensure_config()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{SALMON}Выход без сохранения настроек.{RESET_STYLE}")
        return

    fm = FileManager(default_start_directory())
    chat_store = ChatStore()
    ai = AIClient(fm, chat_store, app_config)
    session = SessionContext()

    print_banner(app_config)
    print(f"{SALMON_DIM}Рабочая папка: {fm.work_dir}{RESET_STYLE}")
    print(f"{SALMON_DIM}Чат: {chat_store.active_name()}{RESET_STYLE}\n")

    while True:
        short = str(fm.work_dir)
        if len(short) > 36:
            short = "..." + short[-33:]
        chat_label = chat_store.active_name()
        if len(chat_label) > 12:
            chat_label = chat_label[:11] + "…"
        ctx_bar = ai.format_context_bar()
        prompt = (
            f"{ctx_bar} {SALMON_DIM}[{chat_label}|{short}]{RESET_STYLE} {SALMON}>{RESET_STYLE} "
        )
        line = read_user_input(prompt)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            if not handle_slash_command(
                *parse_command(line), fm, ai, chat_store, app_config
            ):
                break
            continue

        if ai.context_full(line):
            used, max_t, pct = ai.context_stats(line)
            print(
                f"{Fore.RED}Контекст {pct:.0f}% ({used:,}/{max_t:,} токенов). "
                f"Ввод заблокирован. /newchat или /clear{Style.RESET_ALL}"
            )
            continue

        leading_path, task_line = split_leading_path_command(line)
        if leading_path:
            cd_msg = fm.change_dir(leading_path, create_if_missing=True)
            print(f"{SALMON}{cd_msg}{RESET_STYLE}")
            ai.refresh_system_prompt()
            line = task_line

        cd_msg = try_change_dir_from_user_message(line, fm, session)
        if cd_msg:
            print(f"{SALMON}{cd_msg}{RESET_STYLE}")
            ai.refresh_system_prompt()
        elif not session.is_continuation(line):
            session.remember_request(line, None)

        print(f"{SALMON_DIM}  ── обработка ──{RESET_STYLE}")
        reply, prepared = ai.chat(line, session)
        if fm.pending_patches:
            print_file_patches(fm.pending_patches)
            fm.clear_patches()
        if prepared:
            print(f"{SALMON_DIM}  ↳ прочитано: {', '.join(prepared)}{RESET_STYLE}")
        print(f"{SALMON}Cortex:{RESET_STYLE} {reply}\n")

    ai.save_history()


if __name__ == "__main__":
    main()
