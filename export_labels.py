#!/usr/bin/env python3
"""
Собирает итоговую разметку в один файл labeled_issues.json.
Берёт ответы лучшей связки (инструкция v3 + GigaChat-2-Max), а там, где
обращение распознаётся простым правилом, правило главнее модели.
Базу не меняет. Запуск: python3 export_labels.py
"""
import json
import sqlite3
from classify import rule_based

NAMES = {"openai/codex": "Codex", "cline/cline": "Cline", "google-gemini/gemini-cli": "Gemini CLI",
         "RooCodeInc/Roo-Code": "Roo Code", "continuedev/continue": "Continue", "Aider-AI/aider": "Aider"}
PAIN = "setup refresh had errors|os error 32|sharing violation|node_repl.exe|ACL validation"   # сбой песочницы Codex на Windows

db = sqlite3.connect("file:issues.db?mode=ro", uri=True)
rows = db.execute("""
    SELECT i.id, i.repo, i.number, i.title, i.url, i.state, i.comments, i.thumbs_up,
           i.created_at, i.closed_at, c.kind, c.topic,
           (i.title || ' ' || COALESCE(i.body, '')) AS text
    FROM issues i
    LEFT JOIN classified c ON c.issue_id = i.id AND c.prompt_version = 'v3' AND c.model = 'GigaChat-2-Max'
    ORDER BY i.repo, i.number""").fetchall()

import re
out = []
for id_, repo, number, title, url, state, comments, up, created, closed, kind, topic, text in rows:
    rule = rule_based(title)
    if rule:
        kind, topic, by = rule[0], rule[1], "rule"
    else:
        by = "GigaChat-2-Max"
    out.append({"id": id_, "tool": NAMES[repo], "number": number, "title": title, "url": url, "state": state,
                "comments": comments, "up": up, "created": created[:10], "closed": (closed or "")[:10],
                "by": by, "kind": kind or "unlabeled", "topic": topic or "unlabeled",
                "win_sandbox": bool(repo == "openai/codex" and re.search(PAIN, text, re.I)),
                "windows": bool(re.search(r"windows|win32|powershell", text, re.I))})
json.dump(out, open("labeled_issues.json", "w"), ensure_ascii=False)
print(f"Сохранено обращений: {len(out)}")
