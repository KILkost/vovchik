# Вовчик (Windows + LM Studio)

Сделано под **Windows**, и теперь бот работает через **LM Studio** (без Ollama).

> Важно: если после обновления бот не стартовал, обнови зависимости: `pip install -r requirements.txt`.

## Что изменено

- Бот использует **LM Studio OpenAI-compatible API** (`http://127.0.0.1:1234/v1`).
- Бот автоматически выбирает **любую доступную загруженную модель** из LM Studio (`GET /models`, берется первая из списка).
- Можно задавать **любой вопрос** — не только фиксированные команды.
- Сохранены функции управления окнами/приложениями, ввода текста, отправки сообщений и анализа экрана.

## Установка (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Подготовка LM Studio

1. Запусти LM Studio.
2. Включи Local Server (OpenAI-compatible API).
3. Загрузи любую модель (и держи её активной).
4. По умолчанию бот ходит в `http://127.0.0.1:1234/v1`.

Если нужен другой адрес:

```powershell
setx LMSTUDIO_BASE_URL "http://127.0.0.1:1234/v1"
setx LMSTUDIO_API_KEY "lm-studio"
```

## Запуск

```powershell
python assistant.py
```

Откроется окно статуса (первым), кнопки: **Старт / Стоп / Выход**.

## Команды

### Управление окнами/приложениями
- `список открытых приложений`
- `переключись на chrome`
- `активируй telegram`
- `запусти vscode`

### Ввод/отправка
- `напечатай привет, как дела`
- `отправь`

### Анализ экрана
- `что на экране`
- `проанализируй экран какая ошибка`

### Любой вопрос к ИИ
Просто говори вопрос в свободной форме, например:
- `объясни простыми словами что такое векторная база`
- `помоги написать письмо клиенту`
- `как ускорить мой ноутбук на windows`

## CLI-проверки

```powershell
python assistant.py --ask "любой вопрос"
python assistant.py --ask-screen "что сейчас на экране?"
python assistant.py --list-voices
```

## Если голос не работает

По умолчанию включен более стабильный для Windows backend `SPEECH_BACKEND=sapi` (PowerShell SAPI).

1. Проверь голоса:
   ```powershell
   python assistant.py --list-voices
   ```
2. Убедись, что в Windows установлен русский голос.
3. Если хочешь вернуться к `pyttsx3`, задай:
   ```powershell
   setx SPEECH_BACKEND "pyttsx3"
   ```
4. По умолчанию fallback через PowerShell SAPI уже активен.
