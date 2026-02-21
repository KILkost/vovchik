import base64
import ctypes
import io
import os
import platform
import re
import subprocess
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import psutil
except ImportError:
    psutil = None
try:
    import pyautogui
except ImportError:
    pyautogui = None
try:
    import pyperclip
except ImportError:
    pyperclip = None
try:
    import pyttsx3
except ImportError:
    pyttsx3 = None
try:
    import requests
except ImportError:
    requests = None
try:
    import speech_recognition as sr
except ImportError:
    sr = None
try:
    from PIL import Image
except ImportError:
    Image = None


@dataclass
class Config:
    wake_word: str = "ассистент"
    language: str = "ru-RU"

    # LM Studio OpenAI-compatible server
    lmstudio_base_url: str = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    lmstudio_api_key: str = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    speech_backend: str = os.getenv("SPEECH_BACKEND", "sapi")


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

        self._is_windows = platform.system() == "Windows"
        self.user32 = ctypes.windll.user32 if self._is_windows else None

        self.recognizer = sr.Recognizer() if sr is not None else None
        self.tts = None
        self._init_tts()

        self._run_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    def _init_tts(self) -> None:
        if self.config.speech_backend == "sapi":
            self.tts = None
            return
        if pyttsx3 is None:
            self.tts = None
            return
        try:
            self.tts = pyttsx3.init()
            self._configure_voice()
        except Exception:
            self.tts = None

    def _configure_voice(self) -> None:
        if self.tts is None:
            return
        voices = self.tts.getProperty("voices")
        selected = None
        for voice in voices:
            meta = f"{voice.name} {voice.id}".lower()
            if "ru" in meta or "russian" in meta or "рус" in meta:
                selected = voice.id
                break
        if selected:
            self.tts.setProperty("voice", selected)
        self.tts.setProperty("rate", 170)
        self.tts.setProperty("volume", 1.0)

    def _set_status(self, text: str) -> None:
        self.status_cb(text)
        self.log_cb(f"[STATUS] {text}")

    def _check_http_client(self) -> tuple[bool, str]:
        if requests is None:
            return False, "Модуль requests не установлен. Выполни: pip install -r requirements.txt"
        return True, "ok"

    def _say_fallback_windows(self, text: str) -> bool:
        if not self._is_windows:
            return False
        ps_script = (
            "Add-Type -AssemblyName System.Speech;"
            "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$speak.Speak([Console]::In.ReadToEnd())"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except Exception:
            return False

    def say(self, text: str) -> None:
        self.log_cb(f"[BOT] {text}")

        # Primary mode for Windows reliability.
        if self.config.speech_backend == "sapi":
            if self._say_fallback_windows(text):
                return

        if self.tts is not None:
            try:
                self.tts.say(text)
                self.tts.runAndWait()
                return
            except Exception:
                self.tts = None

        # Lazy pyttsx3 retry in case backend is enabled.
        if self.config.speech_backend != "sapi" and pyttsx3 is not None and self.tts is None:
            try:
                self.tts = pyttsx3.init()
                self._configure_voice()
                self.tts.say(text)
                self.tts.runAndWait()
                return
            except Exception:
                self.tts = None

        self._say_fallback_windows(text)

    def listen(self, timeout: int = 5, phrase_time_limit: int = 10) -> str:
        if sr is None or self.recognizer is None:
            self.log_cb("[ERR] Модуль speech_recognition не установлен")
            return ""
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

    # ---------------- Windows windows/app control ----------------
    def _enumerate_windows_windows(self) -> list[dict]:
        if not self._is_windows:
            return []

        windows: list[dict] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True

            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            buffer = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True

            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            process_name = ""
            exe_path = ""
            try:
                if psutil is not None:
                    proc = psutil.Process(pid.value)
                    process_name = proc.name()
                    exe_path = proc.exe()
            except Exception:
                pass

            windows.append(
                {
                    "window_id": int(hwnd),
                    "pid": int(pid.value),
                    "process": process_name,
                    "exe": exe_path,
                    "title": title,
                }
            )
            return True

        self.user32.EnumWindows(WNDENUMPROC(callback), 0)
        return windows

    def list_open_apps_detailed(self) -> list[dict]:
        return self._enumerate_windows_windows()

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
            exe = self._normalize(entry.get("exe", ""))
            hay = [title, process, exe]
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
        return ranked[0] if ranked and score(ranked[0]) > 0 else None

    def _activate_hwnd_windows(self, hwnd: int) -> bool:
        if not self._is_windows:
            return False
        SW_RESTORE = 9
        try:
            self.user32.ShowWindow(hwnd, SW_RESTORE)
            return bool(self.user32.SetForegroundWindow(hwnd))
        except Exception:
            return False

    def activate_window(self, title_part: str) -> bool:
        entry = self._find_best_window_entry(title_part)
        if entry and entry.get("window_id") and self._activate_hwnd_windows(entry["window_id"]):
            return True

        query_n = self._normalize(title_part)
        for row in self.list_open_apps_detailed():
            title_n = self._normalize(row.get("title", ""))
            process_n = self._normalize(row.get("process", ""))
            if query_n and (query_n in title_n or query_n in process_n):
                if self._activate_hwnd_windows(row["window_id"]):
                    return True
        return False

    def launch_application(self, app_name: str) -> tuple[bool, str]:
        app_name = app_name.strip()
        if not app_name:
            return False, "Не указано имя приложения"

        aliases = {
            "браузер": "msedge",
            "edge": "msedge",
            "хром": "chrome",
            "chrome": "chrome",
            "телеграм": "telegram",
            "telegram": "telegram",
            "дискорд": "discord",
            "discord": "discord",
            "vscode": "code",
            "код": "code",
            "блокнот": "notepad",
            "калькулятор": "calc",
            "проводник": "explorer",
        }
        candidate = aliases.get(app_name.lower(), app_name)

        try:
            subprocess.Popen(["cmd", "/c", "start", "", candidate], shell=False)
            return True, f"Запускаю {app_name}"
        except Exception:
            pass

        try:
            subprocess.Popen([candidate], shell=True)
            return True, f"Запускаю {app_name}"
        except Exception as exc:
            return False, f"Не удалось запустить '{app_name}': {exc}"

    # ---------------- Input actions ----------------
    def type_text(self, text: str) -> bool:
        if not text or pyperclip is None or pyautogui is None:
            return False
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        return True

    def send_message(self) -> None:
        if pyautogui is None:
            return
        pyautogui.press("enter")

    def press_hotkey(self, *keys: str) -> None:
        if pyautogui is None:
            return
        pyautogui.hotkey(*keys)

    # ---------------- LM Studio AI ----------------
    def _lmstudio_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.lmstudio_api_key}",
            "Content-Type": "application/json",
        }

    def _lmstudio_url(self, path: str) -> str:
        return self.config.lmstudio_base_url.rstrip("/") + path

    def check_lmstudio(self) -> tuple[bool, str]:
        ok, msg = self._check_http_client()
        if not ok:
            return False, msg
        try:
            resp = requests.get(self._lmstudio_url("/models"), headers=self._lmstudio_headers(), timeout=8)
            resp.raise_for_status()
            models = resp.json().get("data", [])
            if not models:
                return False, "LM Studio доступен, но нет загруженных моделей"
            return True, f"LM Studio готов. Моделей: {len(models)}"
        except Exception as exc:
            return False, f"Не удалось подключиться к LM Studio: {exc}"

    def _get_active_lmstudio_model(self) -> Optional[str]:
        if requests is None:
            return None
        resp = requests.get(self._lmstudio_url("/models"), headers=self._lmstudio_headers(), timeout=8)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        if not models:
            return None
        return models[0].get("id")

    def ask_ai(self, question: str, image_b64: Optional[str] = None) -> str:
        try:
            model_id = self._get_active_lmstudio_model()
            if not model_id:
                return "В LM Studio нет загруженной модели"

            endpoint = self._lmstudio_url("/chat/completions")
            if image_b64:
                data_url = f"data:image/png;base64,{image_b64}"
                content = [
                    {
                        "type": "text",
                        "text": (
                            "Отвечай только на русском языке, кратко и по делу. "
                            f"Вопрос пользователя: {question}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
            else:
                content = (
                    "Ты голосовой помощник Вовчик. Отвечай по-русски, ясно и полезно. "
                    f"Вопрос пользователя: {question}"
                )

            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0.3,
            }

            resp = requests.post(endpoint, headers=self._lmstudio_headers(), json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(message, list):
                chunks = [part.get("text", "") for part in message if isinstance(part, dict) and part.get("type") == "text"]
                message = "\n".join(chunks)
            answer = str(message).strip()
            return answer or "Модель не вернула ответ"
        except Exception as exc:
            return f"Ошибка обращения к LM Studio: {exc}"

    def take_screenshot(self):
        if pyautogui is None:
            raise RuntimeError("pyautogui не установлен")
        return pyautogui.screenshot()

    def ask_about_screen(self, question: str) -> str:
        image = self.take_screenshot()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return self.ask_ai(question, image_b64=img_b64)

    # ---------------- Intents ----------------
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

        # Всё, что не команда управления окнами/вводом — обычный вопрос к ИИ.
        return "ask_ai", c

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
                        f"[APP] title='{item.get('title','')}' process='{item.get('process','')}' exe='{item.get('exe','')}' pid={item.get('pid')}"
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
                        f"[APP {idx}] title='{item.get('title','')}' process='{item.get('process','')}' exe='{item.get('exe','')}' pid={item.get('pid')}"
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
            ok, msg = self.check_lmstudio()
            if not ok:
                self.say(msg)
                return
            self._set_status("Анализирую экран...")
            self.say("Смотрю на экран")
            answer = self.ask_about_screen(value)
            self.say(answer)
            self._set_status("Готов к командам")
            return

        if intent == "ask_ai":
            ok, msg = self.check_lmstudio()
            if not ok:
                self.say(msg)
                return
            self._set_status("Думаю...")
            answer = self.ask_ai(value)
            self.say(answer)
            self._set_status("Готов к командам")
            return

        if intent == "exit":
            self.say("Останавливаюсь")
            self.stop()
            return

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
        self.root.geometry("760x460")
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
            "Windows + LM Studio: 'список открытых приложений', 'переключись на chrome', "
            "'запусти telegram', 'напечатай ...', 'отправь', 'проанализируй экран ...' "
            "или просто задай любой вопрос боту."
        )
        tk.Label(self.root, text=hint, wraplength=730, fg="#666").pack(anchor="w", padx=10, pady=(0, 8))

        tk.Label(self.root, text="Лог:", font=("Arial", 10, "bold")).pack(anchor="w", padx=10)
        self.log_widget = tk.Text(self.root, height=20, wrap="word")
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.assistant = VoiceAssistant(status_cb=self.set_status, log_cb=self.append_log)
        self.set_status("Готов. Нажми 'Старт' для запуска микрофона")
        missing = []
        if sr is None:
            missing.append("speech_recognition")
        if pyautogui is None:
            missing.append("pyautogui")
        if pyperclip is None:
            missing.append("pyperclip")
        if requests is None:
            missing.append("requests")
        if missing:
            self.append_log("[WARN] Не установлены модули: " + ", ".join(missing) + ". Выполни: pip install -r requirements.txt")

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
    if pyttsx3 is None:
        print("pyttsx3 не установлен")
        return
    tts = pyttsx3.init()
    voices = tts.getProperty("voices")
    for voice in voices:
        print(f"id={voice.id} | name={voice.name}")


def print_usage() -> None:
    print("Использование:")
    print("  python assistant.py")
    print("  python assistant.py --list-voices")
    print('  python assistant.py --ask-screen "что на экране"')
    print('  python assistant.py --ask "любой вопрос"')


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
        ok, msg = assistant.check_lmstudio()
        if not ok:
            print(msg)
            raise SystemExit(2)
        print(assistant.ask_about_screen(question))
        raise SystemExit(0)

    if "--ask" in sys.argv:
        assistant = VoiceAssistant()
        question = parse_cli_value("--ask")
        if not question:
            print_usage()
            raise SystemExit(1)
        ok, msg = assistant.check_lmstudio()
        if not ok:
            print(msg)
            raise SystemExit(2)
        print(assistant.ask_ai(question))
        raise SystemExit(0)

    app = BotControlUI()
    app.run()
