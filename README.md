# Вовчик (Windows + LM Studio)

Текущая версия полностью под **Windows** и с ИИ через **LM Studio**.

## Что улучшено

- Авто-поиск LM Studio (localhost/127.0.0.1, популярные порты, `/v1`) с fallback без Authorization.
- Ввод текста: сначала вставка через буфер, если не получилось — печать по символам.
- Удаление текста: команды очистки текста.
- Закрытие выбранного окна.
- Запуск приложений **только с рабочего стола** (ярлыки `.lnk/.url` и `.exe` на Desktop/Public Desktop).
- Темный UI с более аккуратным современным видом.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Настройка LM Studio

1. Открой LM Studio.
2. Включи **Local Server**.
3. Загрузи модель.
4. Запусти бота. Он сам попробует найти endpoint.

Опционально можно задать вручную:

```powershell
setx LMSTUDIO_BASE_URL "http://127.0.0.1:1234/v1"
setx LMSTUDIO_API_KEY "lm-studio"
```

## Запуск

```powershell
python assistant.py
```

## Примеры команд

### Окна / программы
- `список открытых приложений`
- `переключись на telegram`
- `закрой окно telegram`
- `запусти телеграм` *(ищет на рабочем столе)*

### Ввод / отправка / стирание
- `напечатай привет`
- `сотри текст`
- `удали весь текст`
- `отправь`

### Экран и ИИ
- `что на экране`
- `проанализируй экран какая ошибка`
- любой вопрос в свободной форме

## CLI

```powershell
python assistant.py --ask "любой вопрос"
python assistant.py --ask-screen "что сейчас на экране?"
python assistant.py --list-voices
```

## Голос

По умолчанию стоит `SPEECH_BACKEND=sapi` (надежнее на Windows).

Если нужно попробовать `pyttsx3`:

```powershell
setx SPEECH_BACKEND "pyttsx3"
```
