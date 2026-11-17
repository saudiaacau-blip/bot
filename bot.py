import difflib
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from config import (
    OWNER,
    REPO,
    TRACK_FOLDER,
    HISTORY_FILES,
    BRANCH,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    STATE_FILE,
    GITHUB_TOKEN,
    MESSAGE_SUFFIX,
    BLOCKED_KEYWORDS,
)

import requests

from github_api import GitHub
from telegram_api import Telegram
from parser import parse_appid, parse_name


github = GitHub(OWNER, REPO, GITHUB_TOKEN)
telegram = Telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)


# --------------------------------------------------------
# State
# --------------------------------------------------------

def load_state():

    try:

        with open(STATE_FILE, "r", encoding="utf8") as f:

            return json.load(f)

    except Exception:

        return {
            "last_commit": ""
        }


def save_state(state):

    with open(STATE_FILE, "w", encoding="utf8") as f:

        json.dump(
            state,
            f,
            indent=4
        )


# --------------------------------------------------------
# Version Parser
# --------------------------------------------------------

VERSION_PATTERNS = [

    r'"version"\s*:\s*([0-9]+)',

    r'"build"\s*:\s*([0-9]+)',

    r'"client_version"\s*:\s*([0-9]+)',

    r'"patch"\s*:\s*([0-9]+)',

    r'"revision"\s*:\s*([0-9]+)',

    r'"value"\s*:\s*([0-9]+)'
]


def find_version(text):

    for pattern in VERSION_PATTERNS:

        m = re.search(pattern, text)

        if m:

            return int(m.group(1))

    numbers = re.findall(r"\d+", text)

    if len(numbers):

        return int(numbers[-1])

    return None


# --------------------------------------------------------
# UTC+3
# --------------------------------------------------------

def format_time(timestr):

    utc = datetime.strptime(
        timestr,
        "%Y-%m-%dT%H:%M:%SZ"
    )

    utc = utc.replace(
        tzinfo=timezone.utc
    )

    moscow = utc.astimezone(
        timezone(
            timedelta(hours=3)
        )
    )

    return moscow.strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# --------------------------------------------------------
# Новые строки в файлах истории (_HISTORY_by_date.txt)
# --------------------------------------------------------

def added_lines(old_text, new_text):

    old_lines = [line for line in old_text.splitlines() if line.strip()]
    new_lines = [line for line in new_text.splitlines() if line.strip()]

    # difflib находит именно ДОБАВЛЕННЫЕ строки, а не просто "всё после
    # старой длины" - так это работает даже если строка была вставлена не
    # строго в конец файла.
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)

    result = []

    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            result.extend(new_lines[j1:j2])

    return result


# --------------------------------------------------------
# Оформление сообщений (Telegram, parse_mode=HTML)
# --------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC\+3\)$")
HEAD_RE = re.compile(r"^(\d+) - (.+)$")
FIELD_RE = re.compile(r"^\[(\w)\]\s*(.+?)\s*=>\s*(.+)$")


LABELS = {
    "v": "v",
    "s": "размер",
    "h": "хэш",
}


def render_message(name, appid, fields, date):

    """
    Всё в одну строчку:

        <b>Dota 2</b> 570 | v 123 → 456 | 2026-10-05 17:40:12 UTC+3

        💙 Я люблю тебя Блю

    fields - список (метка, старое, новое), метки: v - версия,
    s - размер, h - хэш.
    """

    head = f"<b>{html.escape(name)}</b>"

    if appid:
        head += f" {html.escape(str(appid))}"

    parts = [head]

    for key, old, new in fields:
        label = LABELS.get(key, key)
        parts.append(
            f"{label} {html.escape(str(old))} → {html.escape(str(new))}"
        )

    if date:
        parts.append(html.escape(date.replace("(UTC+3)", "UTC+3")))

    body = " | ".join(parts)

    return f"{body}\n\n{html.escape(MESSAGE_SUFFIX)}"


def make_history_message(line):

    parts = [p.strip() for p in line.split(" | ")]

    head = parts[0]
    appid = None
    name = head

    m = HEAD_RE.match(head)

    if m:
        appid, name = m.group(1), m.group(2)

    fields = []
    date = ""

    for part in parts[1:]:

        if DATE_RE.match(part):
            date = part
            continue

        fm = FIELD_RE.match(part)

        if fm:
            fields.append((fm.group(1), fm.group(2), fm.group(3)))

    # Строка в незнакомом формате - шлём как есть, просто аккуратно
    if not fields:
        return f"{html.escape(line)}\n\n{html.escape(MESSAGE_SUFFIX)}"

    return render_message(name, appid, fields, date)


# --------------------------------------------------------
# Стоп-слова: такие уведомления не отправляем вовсе
# --------------------------------------------------------

def is_blocked(text):

    lowered = text.lower()

    for keyword in BLOCKED_KEYWORDS:
        if keyword.lower() in lowered:
            return True

    return False


def make_message(file_name, old, new, date):

    appid = parse_appid(file_name)
    name = parse_name(appid)

    return render_message(
        name,
        appid,
        [("v", old, new)],
        f"{date} (UTC+3)"
    )


# --------------------------------------------------------
# Should we even look at this file?
# --------------------------------------------------------

def is_tracked(path):

    # Файл должен лежать непосредственно в TRACK_FOLDER (а не в папке с
    # похожим именем вроде "steam_apiary/...").
    if path != TRACK_FOLDER and not path.startswith(TRACK_FOLDER + "/"):
        return False

    # Интересуют только json-файлы конкретных приложений, служебные файлы
    # вроде _HISTORY_by_date.txt пропускаем - в них нет ключа версии, и без
    # этой проверки find_version() хватал бы случайное число из текста.
    if not path.endswith(".json"):
        return False

    return True


# --------------------------------------------------------
# Отправка (без глушения ошибок!)
# --------------------------------------------------------

def notify(msg):

    # Любая ошибка Telegram пробрасывается наверх: коммит считается
    # необработанным, state.json не сдвигается, запуск помечается красным
    # и при следующем запуске отправка повторится. Раньше ошибка тихо
    # печаталась, state.json уходил вперёд и сообщения терялись навсегда.
    telegram.send(msg, parse_mode="HTML")
    print(msg)

    # Небольшая пауза между сообщениями (flood control Telegram).
    time.sleep(1)


def raw_or_empty(path, ref):

    # "Файла не было в родительском коммите" - это только 404. Любую другую
    # ошибку (сеть, 5xx, лимиты) нельзя считать "файл пуст": иначе вся
    # история из тысяч строк была бы принята за новые строки и отправлена.
    try:
        return github.raw(path, ref)

    except requests.HTTPError as e:

        if e.response is not None and e.response.status_code == 404:
            return ""

        raise


# --------------------------------------------------------
# Обработка одного коммита
# --------------------------------------------------------

def process_commit(commit):

    sha = commit["sha"]

    parents = commit.get("parents") or []

    if not parents:
        print(f"Skipping {sha}: no parent commit")
        return

    parent_sha = parents[0]["sha"]

    files = github.commit(sha).get("files", [])

    paths = [file["filename"] for file in files]

    # Если коммит тронул файл истории, то все версии из него придут
    # строками истории. Отдельно разбирать json-файлы в таком коммите не
    # нужно: это давало дубли (одно и то же обновление приходило дважды).
    history_touched = any(path in HISTORY_FILES for path in paths)

    for path in paths:

        if path in HISTORY_FILES:

            old_text = raw_or_empty(path, parent_sha)
            new_text = github.raw(path, sha)

            for line in added_lines(old_text, new_text):

                if is_blocked(line):
                    print(f"Пропущено (стоп-слово): {line}")
                    continue

                notify(make_history_message(line))

            continue

        if history_touched:
            continue

        if not is_tracked(path):
            continue

        try:
            old_text = github.raw(path, parent_sha)
            new_text = github.raw(path, sha)

        except Exception as e:
            print("RAW ERROR:", e)
            continue

        old_version = find_version(old_text)
        new_version = find_version(new_text)

        if old_version is None or new_version is None:
            continue

        if old_version == new_version:
            continue

        msg = make_message(
            path,
            old_version,
            new_version,
            format_time(commit["commit"]["author"]["date"])
        )

        if is_blocked(msg) or is_blocked(parse_name(parse_appid(path))):
            print(f"Пропущено (стоп-слово): {path}")
            continue

        notify(msg)


# --------------------------------------------------------
# Main
# --------------------------------------------------------

def main():

    state = load_state()

    latest = github.latest_commit(BRANCH)
    latest_sha = latest["sha"]

    if state.get("last_commit") == latest_sha:
        print("No updates.")
        return

    old_sha = state.get("last_commit") or ""

    if not old_sha:
        parents = latest.get("parents") or []
        old_sha = parents[0]["sha"] if parents else latest_sha

    compare = github.compare(old_sha, latest_sha)

    commits = compare.get("commits", [])

    print(f"Found commits: {len(commits)}")

    # Последний ПОЛНОСТЬЮ обработанный коммит. Пока ничего не обработано,
    # состояние остаётся прежним.
    last_ok = state.get("last_commit") or ""

    failed = False

    for commit in commits:

        try:
            process_commit(commit)

        except Exception as e:
            print(
                f"::error::Не удалось обработать коммит {commit['sha']}: {e}",
                file=sys.stderr
            )
            failed = True
            break

        last_ok = commit["sha"]

    # Сдвигаем состояние только до последнего обработанного коммита (а не
    # до latest_sha): так ничего не теряется ни при сбое отправки, ни когда
    # GitHub отдал в сравнении только часть коммитов (по умолчанию 30) -
    # остальные будут обработаны на следующем запуске.
    if commits:
        state["last_commit"] = last_ok
    else:
        state["last_commit"] = latest_sha

    save_state(state)

    if failed:
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":

    try:
        main()

    except SystemExit:
        raise

    except Exception as e:
        # Явно завершаем с ошибкой, чтобы GitHub Actions пометил запуск
        # как failed и это было видно во вкладке Actions.
        print(f"::error::Bot crashed: {e}", file=sys.stderr)
        sys.exit(1)
