#!/usr/bin/env python3
"""
Выгружает обращения (issues) открытых ИИ-инструментов для кода с GitHub
и складывает их в базу данных SQLite (файл issues.db).

Запуск:            python3 fetch_issues.py
Больше страниц:    python3 fetch_issues.py --pages 20
С токеном GitHub:  GITHUB_TOKEN=xxx python3 fetch_issues.py --pages 50

Без токена GitHub разрешает 60 запросов в час, поэтому по умолчанию
берём 6 инструментов по 9 страниц (100 записей на странице) = 54 запроса.
Скрипт можно запускать повторно: уже скачанное не дублируется.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

API = os.environ.get("API_BASE", "https://api.github.com")
TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Инструменты, которые исследуем: (репозиторий, тип инструмента)
REPOS = [
    ("cline/cline", "расширение для редактора"),
    ("continuedev/continue", "расширение для редактора"),
    ("RooCodeInc/Roo-Code", "расширение для редактора"),
    ("Aider-AI/aider", "агент в терминале"),
    ("openai/codex", "агент в терминале"),
    ("google-gemini/gemini-cli", "агент в терминале"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    repo      TEXT PRIMARY KEY,   -- например cline/cline
    tool_type TEXT                -- тип инструмента
);
CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY,  -- уникальный номер обращения на GitHub
    repo        TEXT,                 -- к какому инструменту относится
    number      INTEGER,              -- номер внутри репозитория
    title       TEXT,                 -- заголовок
    body        TEXT,                 -- текст обращения
    state       TEXT,                 -- open (открыто) или closed (закрыто)
    author      TEXT,                 -- кто написал
    created_at  TEXT,                 -- когда создано
    closed_at   TEXT,                 -- когда закрыто (пусто, если ещё открыто)
    comments    INTEGER,              -- сколько комментариев
    reactions   INTEGER,              -- сколько реакций всего
    thumbs_up   INTEGER,              -- сколько реакций «палец вверх»
    url         TEXT                  -- ссылка на обращение
);
CREATE TABLE IF NOT EXISTS labels (
    issue_id INTEGER,                 -- id обращения из таблицы issues
    label    TEXT,                    -- метка, которую поставила команда
    PRIMARY KEY (issue_id, label)
);
"""


class RateLimited(Exception):
    pass


def get(url):
    """Скачивает одну страницу через curl. Возвращает (код ответа, данные)."""
    cmd = ["curl", "-sS", "-L", "--max-time", "60",
           "-H", "Accept: application/vnd.github+json",
           "-H", "User-Agent: ai-devtools-research",
           "-w", "\n%{http_code}"]
    if TOKEN:
        cmd += ["-H", "Authorization: Bearer " + TOKEN]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("curl не смог скачать страницу: " + out.stderr.strip())
    text, _, code = out.stdout.rpartition("\n")
    code = int(code)
    try:
        data = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        data = None
    if code in (403, 429) and isinstance(data, dict) and "rate limit" in str(data.get("message", "")).lower():
        raise RateLimited()
    return code, data


def save_page(db, repo, items):
    """Кладёт страницу обращений в базу. Возвращает, сколько обращений сохранено."""
    saved = 0
    for it in items:
        if "pull_request" in it:      # это не обращение, а предложение изменений в код
            continue
        reactions = it.get("reactions") or {}
        db.execute(
            "INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["id"], repo, it["number"], it.get("title"), it.get("body"),
             it.get("state"), (it.get("user") or {}).get("login"),
             it.get("created_at"), it.get("closed_at"), it.get("comments", 0),
             reactions.get("total_count", 0), reactions.get("+1", 0),
             it.get("html_url")))
        db.execute("DELETE FROM labels WHERE issue_id = ?", (it["id"],))
        for lb in it.get("labels") or []:
            name = lb["name"] if isinstance(lb, dict) else str(lb)
            db.execute("INSERT OR IGNORE INTO labels VALUES (?,?)", (it["id"], name))
        saved += 1
    db.commit()
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=9, help="сколько страниц брать на инструмент")
    ap.add_argument("--db", default="issues.db")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    for repo, tool_type in REPOS:
        db.execute("INSERT OR REPLACE INTO repos VALUES (?,?)", (repo, tool_type))
    db.commit()

    try:
        for repo, _ in REPOS:
            total = 0
            for page in range(1, args.pages + 1):
                url = (f"{API}/repos/{repo}/issues?state=all&sort=created"
                       f"&direction=desc&per_page=100&page={page}")
                code, data = get(url)
                if code != 200 or not isinstance(data, list):
                    print(f"  {repo}: страница {page} не скачалась (код {code}), пропускаю инструмент")
                    break
                if not data:
                    break
                total += save_page(db, repo, data)
                print(f"  {repo}: страница {page}, всего обращений {total}")
                if len(data) < 100:
                    break
                time.sleep(0.5)
    except RateLimited:
        print("\nGitHub попросил подождать: лимит запросов на этот час исчерпан.")
        print("Всё скачанное сохранено. Запусти скрипт ещё раз через час, он докачает остальное.")

    n = db.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    print(f"\nГотово. В базе {args.db} сейчас {n} обращений.")
    for repo, cnt in db.execute("SELECT repo, COUNT(*) FROM issues GROUP BY repo ORDER BY 2 DESC"):
        print(f"  {repo}: {cnt}")


if __name__ == "__main__":
    sys.exit(main())
