#!/usr/bin/env python3
"""
Проверка агента: сравнивает его ответы с эталоном (gold_labels.json)
и печатает, как часто он прав, для каждой пары «инструкция + модель».

Запуск:  python3 eval.py
Ничего не меняет, только читает базу.
"""
import json
import sqlite3
from collections import Counter

gold = {g["issue_id"]: g for g in json.load(open("gold_labels.json"))}
db = sqlite3.connect("issues.db")
rows = db.execute("""
    SELECT prompt_version, model, issue_id, kind, topic
    FROM classified
    WHERE model != 'rule'""").fetchall()

runs = {}
for version, model, issue_id, kind, topic in rows:
    if issue_id in gold:
        runs.setdefault((version, model), {})[issue_id] = (kind, topic)

if not runs:
    raise SystemExit("Агент ещё не размечал эталонную сотню. Сначала: python3 classify.py --gold")

print(f"Эталон: {len(gold)} обращений.\n")
print(f"{'инструкция':11} {'модель':16} {'размечено':>9} {'тип':>6} {'тема':>6} {'оба':>6} {'сбой':>5}")
summary = []
for (version, model), answers in sorted(runs.items()):
    n = len(answers)
    kind_ok = sum(answers[i][0] == gold[i]["kind"] for i in answers)
    gt = lambda i: gold[i].get("topic_" + version, gold[i]["topic"])   # у третьей версии свой список тем
    topic_ok = sum(answers[i][1] == gt(i) for i in answers)
    both_ok = sum(answers[i] == (gold[i]["kind"], gt(i)) for i in answers)
    bad = sum(answers[i][0] == "unparsed" for i in answers)
    print(f"{version:11} {model:16} {n:>9} {kind_ok/n:>6.0%} {topic_ok/n:>6.0%} {both_ok/n:>6.0%} {bad:>5}")
    summary.append({"prompt": version, "model": model, "n": n, "kind_acc": round(kind_ok/n, 3),
                    "topic_acc": round(topic_ok/n, 3), "both_acc": round(both_ok/n, 3), "unparsed": bad})

print("\nСамые частые ошибки в теме (эталон -> ответ агента):")
for (version, model), answers in sorted(runs.items()):
    gt = lambda i: gold[i].get("topic_" + version, gold[i]["topic"])
    wrong = Counter((gt(i), answers[i][1]) for i in answers if answers[i][1] != gt(i))
    top = ", ".join(f"{g} -> {a}: {c}" for (g, a), c in wrong.most_common(5))
    print(f"  {version} {model}: {top or 'ошибок нет'}")

json.dump(summary, open("eval_results.json", "w"), ensure_ascii=False, indent=1)
print("\nИтог сохранён в eval_results.json")
