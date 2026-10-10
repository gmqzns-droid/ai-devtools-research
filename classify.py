#!/usr/bin/env python3
"""
Агент-классификатор: читает обращения из issues.db и для каждого определяет
  kind  - что это (баг, просьба о фиче, вопрос, мусор...)
  topic - о чём оно (установка, модели, поведение агента, интерфейс...)
Результат пишет в таблицу classified в той же базе.

Пробный запуск (20 обращений):   python3 classify.py
Сколько нужно:                    python3 classify.py --limit 200
Все обращения:                    python3 classify.py --all
Другая модель:                    python3 classify.py --model GigaChat-2-Pro
Только эталонная сотня:           python3 classify.py --gold
Другая версия инструкции:         python3 classify.py --gold --prompt v2

Можно прервать (Ctrl+C) и запустить снова: уже размеченное не повторяется.
Ключ GigaChat берётся из файла .env (строка GIGACHAT_KEY=...).
"""
import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import uuid

AUTH_URL = os.environ.get("GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
API_URL = os.environ.get("GIGACHAT_API_URL", "https://gigachat.devices.sberbank.ru/api/v1")
CERT_URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
CERT_FILE = "russian_trusted_root_ca.pem"   # сертификат Минцифры, без него Мак не доверяет серверам Сбера

BODY_LIMIT = 1500          # сколько символов текста обращения отдаём модели

KINDS = {
    "bug": "что-то сломано или работает не так, как должно",
    "feature": "просьба добавить новую возможность или улучшить существующую",
    "question": "вопрос, просьба о помощи, человек не понимает, как пользоваться",
    "junk": "спам, тест, пустое или бессмысленное обращение, не про продукт",
}
TOPICS = {
    "install_auth": "установка, запуск, обновление, вход в аккаунт, авторизация",
    "models": "подключение моделей и провайдеров, выбор модели, ошибки API модели, поддержка новых моделей",
    "agent_behavior": "агент делает не то: ошибки в вызове инструментов, правке файлов, выполнении команд, зацикливание, игнорирует инструкции",
    "ui": "интерфейс: отображение, кнопки, клавиатура, вёрстка, уведомления",
    "stability": "падения, зависания, утечки памяти, медленная работа",
    "sandbox_security": "песочница, разрешения, подтверждения действий, безопасность, приватность",
    "integrations": "MCP-серверы, расширения, плагины, хуки, связка с редактором или другими программами",
    "context_sessions": "контекст, память, история, сессии, сжатие диалога",
    "billing_limits": "оплата, подписка, лимиты, расход токенов и денег",
    "docs": "документация: чего-то нет, устарело или непонятно",
    "other": "ничего из перечисленного не подходит",
}

PROMPT_V1 = (
    "Ты размечаешь обращения пользователей ИИ-инструментов для программирования.\n"
    "Определи для обращения два признака.\n\n"
    "kind (что это):\n" + "\n".join(f"- {k}: {v}" for k, v in KINDS.items()) + "\n\n"
    "topic (о чём):\n" + "\n".join(f"- {k}: {v}" for k, v in TOPICS.items()) + "\n\n"
    "Правила:\n"
    "- выбери ровно одно значение kind и одно значение topic из списков выше;\n"
    "- если подходит несколько тем, выбери ту, в которой причина проблемы, а не её следствие;\n"
    "- для kind=junk ставь topic=other;\n"
    "- ответь только JSON без пояснений, например: {\"kind\": \"bug\", \"topic\": \"ui\"}"
)

# Вторая версия инструкции. Что изменено после разбора ошибок первой:
# 1) модель ставила тему models почти всему, где упомянута какая-нибудь модель,
#    поэтому для models добавлено жёсткое условие;
# 2) добавлены примеры на темы, которые она путала;
# 3) добавлены признаки мусора.
# Примеры придуманы, а не взяты из эталонной сотни, иначе проверка была бы нечестной.
PROMPT_V2 = (
    "Ты размечаешь обращения пользователей ИИ-инструментов для программирования.\n"
    "Определи для обращения два признака.\n\n"
    "kind (что это):\n" + "\n".join(f"- {k}: {v}" for k, v in KINDS.items()) + "\n\n"
    "topic (о чём):\n" + "\n".join(f"- {k}: {v}" for k, v in TOPICS.items()) + "\n\n"
    "Как выбирать topic:\n"
    "- выбирай тему по причине проблемы, а не по словам в тексте;\n"
    "- models ставь, только если дело в подключении к модели или поставщику, в выборе модели "
    "или в ошибке, которую вернула сама модель. Модель упоминается почти в каждом обращении, "
    "само упоминание ничего не значит;\n"
    "- программа не запускается, не устанавливается, не обновляется или не пускает в аккаунт: install_auth;\n"
    "- падает, зависает, тормозит, расходует память: stability;\n"
    "- что-то неправильно показано, не нажимается, пропадает с экрана: ui;\n"
    "- разрешения, подтверждения, песочница, утечка данных, уязвимость: sandbox_security;\n"
    "- навыки, серверы инструментов MCP, расширения, связка с редактором: integrations;\n"
    "- агент неправильно вызвал инструмент, испортил файл, не послушался инструкции: agent_behavior.\n\n"
    "Как выбирать kind:\n"
    "- junk: пустой текст, одно слово, тест, реклама стороннего сервиса, просьба не про продукт, "
    "служебное сообщение робота. Для junk всегда topic=other;\n"
    "- question: человек спрашивает, как что-то сделать или будет ли функция;\n"
    "- feature: просят добавить или улучшить, даже если написано раздражённо;\n"
    "- bug: всё остальное, где что-то работает не так.\n\n"
    "Примеры:\n"
    "Сжатие диалога сбрасывается после возобновления задачи -> {\"kind\": \"bug\", \"topic\": \"context_sessions\"}\n"
    "Телеметрия отправляется, хотя отключена в настройках -> {\"kind\": \"bug\", \"topic\": \"sandbox_security\"}\n"
    "Добавьте поддержку новой модели у такого-то поставщика -> {\"kind\": \"feature\", \"topic\": \"models\"}\n"
    "После обновления программа не запускается -> {\"kind\": \"bug\", \"topic\": \"install_auth\"}\n"
    "Агент удалил не тот файл -> {\"kind\": \"bug\", \"topic\": \"agent_behavior\"}\n"
    "Установленные навыки не видны в списке -> {\"kind\": \"bug\", \"topic\": \"integrations\"}\n"
    "Кнопка уезжает за край окна -> {\"kind\": \"bug\", \"topic\": \"ui\"}\n"
    "Заголовок test, текста нет -> {\"kind\": \"junk\", \"topic\": \"other\"}\n\n"
    "Ответь только JSON без пояснений, например: {\"kind\": \"bug\", \"topic\": \"ui\"}"
)

# Третья версия. Разбор ошибок второй показал, что часть промахов не вина модели:
# сами темы пересекались. Поэтому здесь поправлен список тем:
# 1) добавлена тема config (настройки), модель сама пыталась её придумать;
# 2) разведены ui и agent_behavior: ui это то, что человек видит и нажимает,
#    agent_behavior это действия самого ИИ во время работы над задачей;
# 3) мусор описан конкретными случаями;
# 4) запрещено придумывать значения не из списка.
TOPICS_V3 = {
    "install_auth": "программа не устанавливается, не запускается, не обновляется или не пускает в аккаунт",
    "models": "подключение к модели или поставщику, выбор модели, ошибка, которую вернула сама модель, просьба добавить модель",
    "agent_behavior": "действия самого ИИ во время работы над задачей: неправильно вызвал инструмент, испортил или не так поправил файл, запустил не ту команду, не послушался инструкции, выдумал",
    "ui": "то, что человек видит и нажимает в окне программы: кнопка не работает, текст пропал или отображается криво, поле ввода, прокрутка, уведомления, история в интерфейсе",
    "stability": "падает, зависает, обрывает соединение, тормозит, расходует память или процессор",
    "sandbox_security": "песочница, разрешения, подтверждения действий, утечка данных, уязвимость",
    "integrations": "навыки, серверы инструментов MCP, расширения, связка с редактором или другими программами",
    "context_sessions": "сохранение и синхронизация сессий, память, сжатие диалога, лимиты контекста",
    "config": "настройки и файлы настроек, профили, файлы с инструкциями для агента",
    "billing_limits": "оплата, подписка, кредиты, лимиты, расход",
    "docs": "документация: чего-то нет, устарело или непонятно",
    "other": "ничего из перечисленного не подходит",
}
PROMPT_V3 = (
    "Ты размечаешь обращения пользователей ИИ-инструментов для программирования.\n"
    "Определи для обращения два признака.\n\n"
    "kind (что это):\n"
    "- junk: текста нет или одно слово; заголовок-шаблон без описания проблемы; реклама или предложение "
    "стороннего сервиса; просьба выполнить задачу (нарисуй, напиши, объедини файлы) вместо сообщения о проблеме; "
    "автоматическое сообщение робота о сбое сборки\n"
    "- question: человек спрашивает, как что-то сделать, можно ли так или будет ли функция\n"
    "- feature: просят добавить или улучшить возможность\n"
    "- bug: что-то работает не так, как должно\n\n"
    "topic (о чём):\n" + "\n".join(f"- {k}: {v}" for k, v in TOPICS_V3.items()) + "\n\n"
    "Правила:\n"
    "- выбирай тему по причине проблемы, а не по словам в тексте. Модель упоминается почти в каждом "
    "обращении, само упоминание не делает тему models;\n"
    "- если сломано то, что человек видит или нажимает, это ui, даже когда рядом упомянут агент;\n"
    "- для kind=junk всегда topic=other;\n"
    "- используй только значения из списков выше, новые не придумывай.\n\n"
    "Примеры:\n"
    "Сжатие диалога сбрасывается после возобновления задачи -> {\"kind\": \"bug\", \"topic\": \"context_sessions\"}\n"
    "Телеметрия отправляется, хотя отключена в настройках -> {\"kind\": \"bug\", \"topic\": \"sandbox_security\"}\n"
    "Добавьте поддержку новой модели у такого-то поставщика -> {\"kind\": \"feature\", \"topic\": \"models\"}\n"
    "После обновления программа не запускается -> {\"kind\": \"bug\", \"topic\": \"install_auth\"}\n"
    "Агент удалил не тот файл -> {\"kind\": \"bug\", \"topic\": \"agent_behavior\"}\n"
    "Кнопка отмены не нажимается после первого сообщения -> {\"kind\": \"bug\", \"topic\": \"ui\"}\n"
    "Хочу отдельный файл настроек для каждого проекта -> {\"kind\": \"feature\", \"topic\": \"config\"}\n"
    "Как вернуть скрытую панель? -> {\"kind\": \"question\", \"topic\": \"ui\"}\n"
    "Предлагаем добавить ссылку на наш сервис -> {\"kind\": \"junk\", \"topic\": \"other\"}\n"
    "Заголовок test, текста нет -> {\"kind\": \"junk\", \"topic\": \"other\"}\n\n"
    "Ответь только JSON без пояснений, например: {\"kind\": \"bug\", \"topic\": \"ui\"}"
)
PROMPTS = {"v1": PROMPT_V1, "v2": PROMPT_V2, "v3": PROMPT_V3}
TOPIC_SETS = {"v1": TOPICS, "v2": TOPICS, "v3": TOPICS_V3}


def curl(args, url):
    """Запрос через curl. Возвращает (код ответа, текст)."""
    cmd = ["curl", "-sS", "--max-time", "90", "-w", "\n%{http_code}"]
    if url.startswith("https://"):
        cmd += ["--cacert", CERT_FILE]
    out = subprocess.run(cmd + args + [url], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    text, _, code = out.stdout.rpartition("\n")
    return int(code), text


def ensure_cert():
    if os.path.exists(CERT_FILE) or not API_URL.startswith("https://"):
        return
    print("Скачиваю сертификат Минцифры (нужен один раз)...")
    out = subprocess.run(["curl", "-sS", "--max-time", "60", "-o", CERT_FILE, CERT_URL],
                         capture_output=True, text=True)
    ok = out.returncode == 0 and os.path.exists(CERT_FILE) and "BEGIN CERTIFICATE" in open(CERT_FILE).read()
    if not ok:
        if os.path.exists(CERT_FILE):
            os.remove(CERT_FILE)
        sys.exit("Не получилось скачать сертификат автоматически.\n"
                 "Скачай корневой сертификат с https://www.gosuslugi.ru/crt (формат PEM)\n"
                 f"и положи его в эту папку под именем {CERT_FILE}.\n" + out.stderr.strip())


def read_key():
    key = os.environ.get("GIGACHAT_KEY", "")
    if not key and os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith("GIGACHAT_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("Не нашёл ключ. В файле .env должна быть строка GIGACHAT_KEY=...")
    return key


class GigaChat:
    def __init__(self, key, model):
        self.key, self.model = key, model
        self.token, self.token_time = None, 0
        self.tokens_used = 0

    def login(self):
        """Меняем постоянный ключ на временный токен (живёт 30 минут)."""
        code, text = curl(["-X", "POST",
                           "-H", "Authorization: Basic " + self.key,
                           "-H", "RqUID: " + str(uuid.uuid4()),
                           "-H", "Content-Type: application/x-www-form-urlencoded",
                           "-H", "Accept: application/json",
                           "--data-urlencode", "scope=GIGACHAT_API_PERS"], AUTH_URL)
        if code != 200:
            sys.exit(f"GigaChat не принял ключ (код {code}): {text[:300]}")
        self.token = json.loads(text)["access_token"]
        self.token_time = time.time()

    def ask(self, system, user):
        """Один вопрос модели. Возвращает текст ответа."""
        payload = json.dumps({
            "model": self.model,
            "temperature": 0,
            "max_tokens": 60,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }, ensure_ascii=False)
        for attempt in range(8):
            if not self.token or time.time() - self.token_time > 25 * 60:
                try:
                    self.login()
                except RuntimeError as e:   # обрыв связи при получении токена
                    print(f"  связь прервалась ({str(e)[:60]}), пробую ещё раз...")
                    time.sleep(15 * (attempt + 1))
                    continue
            try:
                code, text = curl(["-X", "POST",
                                   "-H", "Authorization: Bearer " + self.token,
                                   "-H", "Content-Type: application/json",
                                   "-H", "Accept: application/json",
                                   "--data-binary", payload], API_URL + "/chat/completions")
            except RuntimeError as e:       # обрыв связи или таймаут: ждём и пробуем снова
                print(f"  связь прервалась ({str(e)[:60]}), пробую ещё раз...")
                time.sleep(15 * (attempt + 1))
                continue
            if code == 200:
                data = json.loads(text)
                self.tokens_used += (data.get("usage") or {}).get("total_tokens", 0)
                return data["choices"][0]["message"]["content"]
            if code == 401:                 # токен протух
                self.token = None
                continue
            if code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"GigaChat ответил ошибкой {code}: {text[:300]}")
        raise RuntimeError("GigaChat не ответил после 8 попыток")


def parse_answer(text, topics=TOPICS):
    """Достаёт kind и topic из ответа модели. Если ответ кривой, вернёт unparsed."""
    m = re.search(r"\{.*?\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    kind = str(data.get("kind", "")).strip().lower()
    topic = str(data.get("topic", "")).strip().lower()
    if kind not in KINDS or topic not in topics:
        return "unparsed", "unparsed"
    return kind, topic


def rule_based(title):
    """Что можно определить без модели. Экономит запросы и точнее модели."""
    if re.match(r"^Uncaught \w+ in .+ line \d+", title or ""):
        return "auto_report", "stability"     # автоматический отчёт о падении (Aider)
    if re.match(r"^Error: .+ - (Unknown error|\d{3})$", title or ""):
        return "auto_report", "models"        # отчёт об ошибке модели, созданный кнопкой (Continue)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--model", default="GigaChat-2")
    ap.add_argument("--db", default="issues.db")
    ap.add_argument("--prompt", default="v1", choices=sorted(PROMPTS), help="версия инструкции")
    ap.add_argument("--gold", action="store_true", help="только эталонная сотня из gold_labels.json")
    args = ap.parse_args()
    PROMPT_VERSION, SYSTEM_PROMPT = args.prompt, PROMPTS[args.prompt]

    db = sqlite3.connect(args.db)
    db.execute("""CREATE TABLE IF NOT EXISTS classified (
        issue_id       INTEGER,   -- id обращения из таблицы issues
        prompt_version TEXT,      -- версия инструкции
        model          TEXT,      -- какая модель размечала (rule = правило без модели)
        kind           TEXT,
        topic          TEXT,
        raw            TEXT,      -- сырой ответ модели, чтобы разбирать ошибки
        PRIMARY KEY (issue_id, prompt_version, model))""")

    # 1. Сначала правила: то, что видно по заголовку, модели не отдаём
    n_rule = 0
    for issue_id, title in db.execute("SELECT id, title FROM issues").fetchall():
        hit = rule_based(title)
        if hit:
            cur = db.execute("INSERT OR IGNORE INTO classified VALUES (?,?,?,?,?,?)",
                             (issue_id, PROMPT_VERSION, "rule", hit[0], hit[1], ""))
            n_rule += cur.rowcount
    db.commit()
    if n_rule:
        print(f"Правилами размечено {n_rule} автоматических отчётов о падениях.")

    # 2. Остальное отдаём модели. Перемешиваем, чтобы в пробе были все инструменты
    todo = db.execute("""
        SELECT id, repo, title, body FROM issues
        WHERE id NOT IN (SELECT issue_id FROM classified
                         WHERE prompt_version = ? AND model IN (?, 'rule'))
        ORDER BY id""", (PROMPT_VERSION, args.model)).fetchall()
    random.Random(42).shuffle(todo)   # одинаковая перемешка при каждом запуске
    if args.gold:
        gold_ids = {g["issue_id"] for g in json.load(open("gold_labels.json"))}
        todo = [t for t in todo if t[0] in gold_ids]
    elif not args.all:
        todo = todo[:args.limit]
    if not todo:
        print("Всё уже размечено.")
        return
    print(f"Отдаю модели {args.model} (инструкция {PROMPT_VERSION}): {len(todo)} обращений.")

    ensure_cert()
    llm = GigaChat(read_key(), args.model)
    done = 0
    try:
        for issue_id, repo, title, body in todo:
            user = f"Инструмент: {repo}\nЗаголовок: {title}\nТекст:\n{(body or '')[:BODY_LIMIT]}"
            raw = llm.ask(SYSTEM_PROMPT, user)
            kind, topic = parse_answer(raw, TOPIC_SETS[PROMPT_VERSION])
            db.execute("INSERT OR REPLACE INTO classified VALUES (?,?,?,?,?,?)",
                       (issue_id, PROMPT_VERSION, args.model, kind, topic, raw))
            db.commit()
            done += 1
            print(f"[{done}/{len(todo)}] {kind:9} {topic:17} {(title or '')[:70]}")
    except KeyboardInterrupt:
        print("\nОстановлено. Размеченное сохранено.")
    except RuntimeError as e:
        print(f"\nОстановился из-за ошибки: {e}\nРазмеченное сохранено, можно запустить снова.")

    print(f"\nРазмечено за этот запуск: {done}. Потрачено токенов: {llm.tokens_used}.")
    bad = db.execute("SELECT COUNT(*) FROM classified WHERE kind='unparsed' AND prompt_version=?",
                     (PROMPT_VERSION,)).fetchone()[0]
    if bad:
        print(f"Ответов, которые не удалось разобрать: {bad}.")


if __name__ == "__main__":
    main()
