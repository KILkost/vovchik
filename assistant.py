import base64
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Optional

import pyautogui
import pygetwindow as gw
import pyperclip
import pyttsx3
import requests
import speech_recognition as sr
from PIL import Image


@dataclass
class Config:
    wake_word: str = "ассистент"
    language: str = "ru-RU"

    # Vision backend: ollama | openai_compatible
    vision_backend: str = os.getenv("VISION_BACKEND", "ollama")
    vision_model: str = os.getenv("VISION_MODEL", "llava")

    # Ollama
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

    # OpenAI-compatible API
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")


class VoiceAssistant:
    def __init__(
        self,
        config: Optional[Config] = None,
        status_cb: Optional[Callable[[str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config or Config()
        self.status_cb = status_cb or (lambda _m: None)
        self.log_cb = log_cb or (lambda _m: None)

        self.recognizer = sr.Recognizer()
        self.tts = pyttsx3.init()
        self._configure_voice()

        self._run_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    def _configure_voice(self) -> None:
        voices = self.tts.getProperty("voices")
        selected = None
        for voice in voices:
            meta = f"{voice.name} {voice.id}".lower()
            if "ru" in meta or "russian" in meta or "рус" in meta:
                selected = voice.id
                break
        if selected:
            self.tts.setProperty("voice", selected)
        self.tts.setProperty("rate", 175)
        self.tts.setProperty("volume", 1.0)

    def _set_status(self, text: str) -> None:
        self.status_cb(text)
        self.log_cb(f"[STATUS] {text}")

    def _say_fallback(self, text: str) -> bool:
        # Fallback to system speech engines when pyttsx3 is unstable.
        candidates = []
        if platform.system() == "Darwin":
            candidates.append(["say", text])
        else:
            candidates.extend((
                ["spd-say", text],
                ["espeak", "-v", "ru", text],
                ["espeak-ng", "-v", "ru", text],
            ))

        for cmd in candidates:
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except Exception:
                    continue
        return False

    def say(self, text: str) -> None:
        self.log_cb(f"[BOT] {text}")
        try:
            self.tts.say(text)
            self.tts.runAndWait()
        except Exception:
            try:
                self.tts = pyttsx3.init()
                self._configure_voice()
                self.tts.say(text)
                self.tts.runAndWait()
            except Exception:
                self._say_fallback(text)

    def listen(self, timeout: int = 5, phrase_time_limit: int = 8) -> str:
        with sr.Microphone() as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        try:
            return self.recognizer.recognize_google(audio, language=self.config.language).lower().strip()
        except sr.UnknownValueError:
            return ""
        except sr.RequestError:
            self.say("Не удалось обратиться к сервису распознавания")
            return ""

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^\wа-яё]+", "", text.casefold(), flags=re.IGNORECASE)

    def _linux_windows_detailed(self) -> list[dict]:
        # Requires wmctrl, gives real PID/process/class/title.
        rows = []
        if not shutil.which("wmctrl"):
            return rows

        proc = subprocess.run(["wmctrl", "-lpGx"], capture_output=True, text=True)
        if proc.returncode != 0:
            return rows

        for line in proc.stdout.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            win_id, _desktop, pid_s, _x, _y, _w, _h, wm_class, title = parts
            pid = int(pid_s) if pid_s.isdigit() else None
            process_name = ""
            if pid:
                try:
                    with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
                        process_name = f.read().strip()
                except Exception:
                    process_name = ""
            rows.append(
                {
                    "window_id": win_id,
                    "pid": pid,
                    "process": process_name,
                    "wm_class": wm_class,
                    "title": title.strip(),
                }
            )
        return rows

    def list_open_windows(self) -> list[str]:
        # Prefer deep Linux view.
        detailed = self._linux_windows_detailed()
        if detailed:
            return [item["title"] for item in detailed if item["title"]]

        titles = []
        for title in gw.getAllTitles():
            cleaned = title.strip()
            if cleaned and cleaned not in titles:
                titles.append(cleaned)
        return titles

    def list_open_apps_detailed(self) -> list[dict]:
        detailed = self._linux_windows_detailed()
        if detailed:
            return detailed

        # Generic fallback when PID/class isn't available.
        return [{"window_id": "", "pid": None, "process": "", "wm_class": "", "title": t} for t in self.list_open_windows()]

    def _find_best_window_entry(self, query: str) -> Optional[dict]:
        query_n = self._normalize(query)
        if not query_n:
            return None

        entries = self.list_open_apps_detailed()
        if not entries:
            return None

        def score(entry: dict) -> int:
            title = self._normalize(entry.get("title", ""))
            process = self._normalize(entry.get("process", ""))
            wm_class = self._normalize(entry.get("wm_class", ""))
            hay = [title, process, wm_class]
            s = 0
            for field in hay:
                if not field:
                    continue
                if field == query_n:
                    s = max(s, 100)
                if query_n in field:
                    s = max(s, 80)
                if any(tok and tok in field for tok in query_n.split()):
                    s = max(s, 50)
            return s

        ranked = sorted(entries, key=score, reverse=True)
        if ranked and score(ranked[0]) > 0:
            return ranked[0]
        return None

    def activate_window(self, title_part: str) -> bool:
        entry = self._find_best_window_entry(title_part)

        # 1) Linux deep activation by exact window id.
        if entry and entry.get("window_id") and shutil.which("wmctrl"):
            proc = subprocess.run(["wmctrl", "-ia", entry["window_id"]], capture_output=True, text=True)
            if proc.returncode == 0:
                return True

        if entry and entry.get("window_id") and shutil.which("xdotool"):
            proc = subprocess.run(["xdotool", "windowactivate", entry["window_id"]], capture_output=True, text=True)
            if proc.returncode == 0:
                return True

        best_title = entry["title"] if entry else title_part

        # 2) pygetwindow path.
        try:
            windows = gw.getWindowsWithTitle(best_title)
            for win in windows:
                try:
                    if win.isMinimized:
                        win.restore()
                    win.activate()
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        # 3) Linux fallback by name.
        if shutil.which("wmctrl"):
            for candidate in (best_title, title_part):
                proc = subprocess.run(["wmctrl", "-a", candidate], capture_output=True, text=True)
                if proc.returncode == 0:
                    return True

        if shutil.which("xdotool"):
            for candidate in (best_title, title_part):
                proc = subprocess.run(
                    ["xdotool", "search", "--name", candidate, "windowactivate"],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode == 0:
                    return True

        return False

    def launch_application(self, app_name: str) -> tuple[bool, str]:
        app_name = app_name.strip()
        if not app_name:
            return False, "Не указано имя приложения"

        aliases = {
            "браузер": "firefox",
            "firefox": "firefox",
            "chrome": "google-chrome",
            "хром": "google-chrome",
            "telegram": "telegram-desktop",
            "телеграм": "telegram-desktop",
            "vscode": "code",
            "код": "code",
        }

        candidate = aliases.get(app_name.lower(), app_name)

        # Exact binary in PATH.
        exe = shutil.which(candidate)
        if exe:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, f"Запускаю {app_name}"

        system_name = platform.system()
        try:
            if system_name == "Linux":
                if shutil.which("gtk-launch"):
                    proc = subprocess.run(["gtk-launch", candidate], capture_output=True, text=True)
                    if proc.returncode == 0:
                        return True, f"Запускаю {app_name}"
                if shutil.which("xdg-open"):
                    # For .desktop id / URL / path
                    subprocess.Popen(["xdg-open", candidate], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True, f"Пробую открыть {app_name}"

            if system_name == "Darwin":
                proc = subprocess.run(["open", "-a", candidate], capture_output=True, text=True)
                if proc.returncode == 0:
                    return True, f"Запускаю {app_name}"

            if system_name == "Windows":
                subprocess.Popen(["cmd", "/c", "start", "", candidate], shell=True)
                return True, f"Запускаю {app_name}"
        except Exception as exc:
            return False, f"Ошибка запуска: {exc}"

        return False, f"Не удалось запустить '{app_name}'. Проверь имя приложения"

    def type_text(self, text: str) -> bool:
        if not text:
            return False
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        return True

    def send_message(self) -> None:
        pyautogui.press("enter")

    def press_hotkey(self, *keys: str) -> None:
        pyautogui.hotkey(*keys)

    def take_screenshot(self) -> Image.Image:
        return pyautogui.screenshot()

    def check_vision_backend(self) -> tuple[bool, str]:
        if self.config.vision_backend == "openai_compatible":
            if not self.config.openai_api_key:
                return False, "Для openai_compatible укажи OPENAI_API_KEY"
            return True, "OpenAI-compatible backend готов"

        # ollama backend
        health_url = self.config.ollama_url.replace("/api/generate", "/api/tags")
        try:
            resp = requests.get(health_url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            if not any(self.config.vision_model in model for model in models):
                return (
                    False,
                    f"Ollama запущен, но модель '{self.config.vision_model}' не найдена. Выполни: ollama pull {self.config.vision_model}",
                )
            return True, "Ollama и модель готовы"
        except Exception as exc:
            return False, f"Не удалось подключиться к Ollama: {exc}"

    def _ask_ollama(self, question: str, img_b64: str) -> str:
        payload = {
            "model": self.config.vision_model,
            "prompt": (
                "Ты помощник Вовчик. Тебе передали реальный скриншот монитора пользователя. "
                "Отвечай строго по содержимому изображения, по-русски, кратко и практично. "
                f"Вопрос: {question}"
            ),
            "images": [img_b64],
            "stream": False,
        }
        resp = requests.post(self.config.ollama_url, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    def _ask_openai_compatible(self, question: str, img_b64: str) -> str:
        endpoint = self.config.openai_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }
        data_url = f"data:image/png;base64,{img_b64}"
        payload = {
            "model": self.config.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Ответь только на русском, кратко и по содержимому скриншота. "
                                f"Вопрос пользователя: {question}"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0.2,
        }
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(message, list):
            chunks = []
            for part in message:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(part.get("text", ""))
            message = "\n".join(chunks)
        return str(message).strip()

    def ask_about_screen(self, question: str) -> str:
        image = self.take_screenshot()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        try:
            if self.config.vision_backend == "openai_compatible":
                answer = self._ask_openai_compatible(question, img_b64)
            else:
                answer = self._ask_ollama(question, img_b64)
            return answer or "Модель не вернула ответ"
        except Exception as exc:
            return f"Ошибка при анализе экрана: {exc}"

    @staticmethod
    def _clean_phrase(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower()).strip()

    @staticmethod
    def _extract_text_after_keywords(command: str, keywords: list[str]) -> str:
        for keyword in keywords:
            if keyword in command:
                suffix = command.split(keyword, 1)[1].strip(" .,!?:;-")
                if suffix:
                    return suffix
        return ""

    def _parse_intent(self, command: str) -> tuple[str, str]:
        c = self._clean_phrase(command)

        if any(word in c for word in ["выход", "стоп", "заверш", "закрой ассистента", "выключи бота"]):
            return "exit", ""

        if any(word in c for word in ["список окон", "какие окна", "открытые приложения", "список приложений"]):
            return "list_apps", ""

        if any(word in c for word in ["запусти", "стартуй", "открой приложение"]):
            app_name = self._extract_text_after_keywords(c, ["запусти", "стартуй", "открой приложение"])
            return "launch_app", app_name

        if any(word in c for word in ["отправ", "send", "вышли"]):
            return "send", ""

        if any(word in c for word in ["экран", "скрин", "что видишь", "анализ"]):
            question = self._extract_text_after_keywords(
                c,
                ["что на экране", "проанализируй экран", "посмотри экран", "видишь на экране", "экран"],
            )
            return "screen", question or "Что находится на экране?"

        if any(word in c for word in ["переключ", "активируй", "открой окно", "сфокусируй"]):
            title = self._extract_text_after_keywords(c, ["открой окно", "переключись на", "активируй", "сфокусируй", "открой"])
            return "window", title

        if any(word in c for word in ["напечат", "введи", "впиши", "набери"]):
            text = self._extract_text_after_keywords(c, ["напечатай", "введи", "впиши", "набери"])
            return "type", text

        if any(word in c for word in ["нажми", "горяч", "комбинац"]):
            raw = self._extract_text_after_keywords(c, ["нажми", "комбинацию", "горячие клавиши"])
            return "hotkey", raw

        return "unknown", c

    def handle_command(self, command: str) -> None:
        intent, value = self._parse_intent(command)

        if intent == "window":
            if value and self.activate_window(value):
                self.say(f"Окно {value} активировано")
            else:
                candidates = self.list_open_apps_detailed()[:12]
                self.say("Не удалось активировать окно. Скажи точнее название")
                for item in candidates:
                    self.log_cb(
                        f"[APP] title='{item.get('title','')}' process='{item.get('process','')}' class='{item.get('wm_class','')}' pid={item.get('pid')}"
                    )
            return

        if intent == "launch_app":
            ok, msg = self.launch_application(value)
            self.say(msg)
            return

        if intent == "list_apps":
            apps = self.list_open_apps_detailed()[:30]
            if not apps:
                self.say("Не вижу открытых окон")
            else:
                self.say(f"Нашел {len(apps)} окон. Показал в логе")
                for idx, item in enumerate(apps, start=1):
                    self.log_cb(
                        f"[APP {idx}] title='{item.get('title','')}' process='{item.get('process','')}' class='{item.get('wm_class','')}' pid={item.get('pid')}"
                    )
            return

        if intent == "type":
            if not value:
                self.say("Не расслышал текст для ввода")
                return
            ok = self.type_text(value)
            self.say("Текст вставлен" if ok else "Не удалось вставить текст")
            return

        if intent == "send":
            self.send_message()
            self.say("Отправил")
            return

        if intent == "hotkey":
            keys = tuple(part.strip() for part in value.split("+") if part.strip())
            if keys:
                self.press_hotkey(*keys)
                self.say("Сделано")
            else:
                self.say("Не понял комбинацию клавиш")
            return

        if intent == "screen":
            ok, msg = self.check_vision_backend()
            if not ok:
                self.say(msg)
                return
            self._set_status("Анализирую экран...")
            self.say("Смотрю на экран")
            answer = self.ask_about_screen(value)
            self.say(answer)
            self._set_status("Готов к командам")
            return

        if intent == "exit":
            self.say("Останавливаюсь")
            self.stop()
            return

        self.say("Пока не понял команду. Попробуй сказать иначе")

    def _loop(self) -> None:
        self._set_status("Слушаю")
        self.say("Бот запущен")
        while self._run_event.is_set():
            try:
                heard = self.listen()
                if not self._run_event.is_set():
                    break
                if not heard:
                    continue

                self.log_cb(f"[YOU] {heard}")

                if self.config.wake_word in heard:
                    heard = heard.replace(self.config.wake_word, "", 1).strip()
                    if not heard:
                        self.say("Слушаю")
                        heard = self.listen(timeout=6, phrase_time_limit=10)

                if heard:
                    self.log_cb(f"[CMD] {heard}")
                    self.handle_command(heard)
            except sr.WaitTimeoutError:
                continue
            except Exception as exc:
                self.log_cb(f"[ERR] {exc}")

        self._set_status("Остановлен")

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._run_event.set()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._run_event.clear()


class BotControlUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Вовчик — статус")
        self.root.geometry("620x420")
        self.root.attributes("-topmost", True)

        self.status_var = tk.StringVar(value="Загрузка бота...")

        tk.Label(self.root, text="Статус:", font=("Arial", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 0))
        tk.Label(self.root, textvariable=self.status_var, fg="#1f6feb", font=("Arial", 11)).pack(anchor="w", padx=10)

        row = tk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=10)
        tk.Button(row, text="Старт", command=self.start_bot, width=12).pack(side="left", padx=4)
        tk.Button(row, text="Стоп", command=self.stop_bot, width=12).pack(side="left", padx=4)
        tk.Button(row, text="Выход", command=self.on_close, width=12).pack(side="left", padx=4)

        hint = (
            "Подсказка: можно сказать 'список открытых приложений', 'запусти telegram', "
            "'напечатай ...', 'отправь', 'проанализируй экран ...'."
        )
        tk.Label(self.root, text=hint, wraplength=590, fg="#666").pack(anchor="w", padx=10, pady=(0, 8))

        tk.Label(self.root, text="Лог:", font=("Arial", 10, "bold")).pack(anchor="w", padx=10)
        self.log_widget = tk.Text(self.root, height=18, wrap="word")
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.assistant = VoiceAssistant(status_cb=self.set_status, log_cb=self.append_log)
        self.set_status("Готов. Нажми 'Старт' для запуска микрофона")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def append_log(self, text: str) -> None:
        def _append() -> None:
            self.log_widget.insert("end", text + "\n")
            self.log_widget.see("end")

        self.root.after(0, _append)

    def start_bot(self) -> None:
        self.assistant.start()
        self.set_status("Запуск...")

    def stop_bot(self) -> None:
        self.assistant.stop()
        self.set_status("Остановлен")

    def on_close(self) -> None:
        self.assistant.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def list_voices() -> None:
    tts = pyttsx3.init()
    voices = tts.getProperty("voices")
    for voice in voices:
        print(f"id={voice.id} | name={voice.name}")


def print_usage() -> None:
    print("Использование:")
    print("  python assistant.py")
    print("  python assistant.py --list-voices")
    print('  python assistant.py --ask-screen "что на экране"')


def parse_cli_value(flag: str) -> Optional[str]:
    if flag not in sys.argv:
        return None
    idx = sys.argv.index(flag)
    if len(sys.argv) <= idx + 1:
        return None
    return sys.argv[idx + 1].strip()


if __name__ == "__main__":
    if "--list-voices" in sys.argv:
        list_voices()
        raise SystemExit(0)

    if "--ask-screen" in sys.argv:
        assistant = VoiceAssistant()
        question = parse_cli_value("--ask-screen")
        if not question:
            print_usage()
            raise SystemExit(1)
        ok, msg = assistant.check_vision_backend()
        if not ok:
            print(msg)
            raise SystemExit(2)
        print(assistant.ask_about_screen(question))
        raise SystemExit(0)

    app = BotControlUI()
    app.run()
