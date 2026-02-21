# Голосовой помощник для ПК (Вовчик)

Теперь Вовчик запускается со статусным окном:
- открывается первым;
- показывает состояние (загрузка / слушаю / анализирую экран / остановлен);
- кнопки **Старт / Стоп / Выход**.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
python assistant.py
```

## Что добавлено и исправлено

- **Глубокий анализ открытых окон** (Linux): кроме заголовка учитываются PID, process name и WM_CLASS через `wmctrl -lpGx`.
- **Улучшенная активация окна**: сначала попытка по `window_id`, затем по title (`pygetwindow`, `wmctrl`, `xdotool`).
- **Запуск неоткрытых приложений**: команда `запусти ...` / `стартуй ...` / `открой приложение ...`.
- **Отправка сообщения**: команда `отправь` / `вышли сообщение` (нажимает Enter).
- **Голос бота**: добавлен fallback TTS (`spd-say` / `espeak` / `espeak-ng` / `say`), если `pyttsx3` нестабилен.
- **Выбор vision-ИИ**: теперь можно использовать не только Ollama, но и OpenAI-compatible API (например OpenRouter с моделями, лучше работающими на русском).

## Примеры команд

### Работа с окнами
- `список открытых приложений`
- `какие окна открыты`
- `переключись на telegram`
- `активируй vscode`

### Запуск приложения
- `запусти telegram`
- `стартуй firefox`
- `открой приложение code`

### Ввод и отправка
- `напечатай привет, как дела`
- `введи завтра встреча в 10`
- `отправь`

### Анализ экрана
- `что на экране`
- `проанализируй экран какая ошибка`
- `посмотри экран и объясни что не так`

## Выбор ИИ для анализа экрана

### Вариант 1: Ollama (по умолчанию)

```bash
ollama serve
ollama pull llava
python assistant.py --ask-screen "что сейчас на экране?"
```

Можно выбрать другую vision-модель:

```bash
export VISION_MODEL="llama3.2-vision"
# или
export VISION_MODEL="qwen2.5vl:7b"
```

### Вариант 2: OpenAI-compatible API (лучше русский)

Пример с OpenRouter/другим совместимым API:

```bash
export VISION_BACKEND="openai_compatible"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_API_KEY="<твой_ключ>"
export VISION_MODEL="qwen/qwen2.5-vl-72b-instruct:free"
python assistant.py --ask-screen "что сейчас на экране?"
```

## Доп. команды

```bash
python assistant.py --list-voices
```

## Примечания

- Для Linux может понадобиться `portaudio` для `PyAudio`.
- Для надежной активации окон на Linux желательно установить:
  - `wmctrl`
  - `xdotool`
- Для fallback озвучки (если pyttsx3 молчит):
  - `speech-dispatcher` (`spd-say`) или
  - `espeak` / `espeak-ng`
