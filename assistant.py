import base64
import io
import os
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
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    vision_model: str = os.getenv("VISION_MODEL", "llava")
    language: str = "ru-RU"


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

    def say(self, text: str) -> None:
        self.log_cb(f"[BOT] {text}")
        try:
            self.tts.say(text)
            self.tts.runAndWait()
        except Exception:
            self.tts = pyttsx3.init()
            self._configure_voice()
            self.tts.say(text)
            self.tts.runAndWait()

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

    def list_open_windows(self) -> list[str]:
        titles = []
        for title in gw.getAllTitles():
            cleaned = title.strip()
            if cleaned and cleaned not in titles:
                titles.append(cleaned)
        return titles

    def _find_best_title(self, query: str) -> Optional[str]:
        query_n = self._normalize(query)
        if not query_n:
            return None
        titles = self.list_open_windows()

        exact = [t for t in titles if self._normalize(t) == query_n]
        if exact:
            return exact[0]

        contains = [t for t in titles if query_n in self._normalize(t)]
        if contains:
            contains.sort(key=len)
            return contains[0]

        partial = [t for t in titles if any(part in self._normalize(t) for part in query_n.split())]
        if partial:
            partial.sort(key=len)
            return partial[0]

        return None

    def activate_window(self, title_part: str) -> bool:
        best = self._find_best_title(title_part) or title_part

        try:
            windows = gw.getWindowsWithTitle(best)
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

        if shutil.which("wmctrl"):
            for candidate in (best, title_part):
                proc = subprocess.run(["wmctrl", "-a", candidate], capture_output=True, text=True)
                if proc.returncode == 0:
                    return True

        if shutil.which("xdotool"):
            for candidate in (best, title_part):
                proc = subprocess.run(
                    ["xdotool", "search", "--name", candidate, "windowactivate"],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode == 0:
                    return True

        return False

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

    def check_ollama(self) -> tuple[bool, str]:
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

    def ask_about_screen(self, question: str) -> str:
        image = self.take_screenshot()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

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

        try:
            resp = requests.post(self.config.ollama_url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            answer = data.get("response", "").strip()
            if not answer:
                return "Модель не вернула ответ"
            return answer
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

        if any(word in c for word in ["отправ", "send", "вышли"]):
            return "send", ""

        if any(word in c for word in ["экран", "скрин", "что видишь", "анализ"]):
            question = self._extract_text_after_keywords(
                c,
                ["что на экране", "проанализируй экран", "посмотри экран", "видишь на экране", "экран"],
            )
            return "screen", question or "Что находится на экране?"

        if any(word in c for word in ["переключ", "активируй", "открой окно", "сфокусируй", "открой приложение"]):
            title = self._extract_text_after_keywords(
                c,
                ["открой окно", "переключись на", "активируй", "сфокусируй", "открой приложение", "открой"],
            )
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
                candidates = self.list_open_windows()[:8]
                self.say("Не удалось активировать окно. Скажи точнее название")
                if candidates:
                    self.log_cb("[INFO] Примеры открытых окон: " + " | ".join(candidates))
            return

        if intent == "list_apps":
            titles = self.list_open_windows()[:20]
            if not titles:
                self.say("Не вижу открытых окон")
            else:
                self.say(f"Вижу {len(titles)} окон. Подробности в статусном окне")
                for idx, title in enumerate(titles, start=1):
                    self.log_cb(f"[APP {idx}] {title}")
            return

        if intent == "type":
            if not value:
                self.say("Не расслышал текст для ввода")
                return
            ok = self.type_text(value)
            if ok:
                self.say("Текст вставлен")
            else:
                self.say("Не удалось вставить текст")
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
            ok, msg = self.check_ollama()
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


def list_voices() -> None:
    tts = pyttsx3.init()
    voices = tts.getProperty("voices")
    for voice in voices:
        print(f"id={voice.id} | name={voice.name}")


class BotControlUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Вовчик — статус")
        self.root.geometry("520x360")
        self.root.attributes("-topmost", True)

        self.status_var = tk.StringVar(value="Загрузка бота...")

        tk.Label(self.root, text="Статус:", font=("Arial", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 0))
        tk.Label(self.root, textvariable=self.status_var, fg="#1f6feb", font=("Arial", 11)).pack(anchor="w", padx=10)

        row = tk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=10)
        tk.Button(row, text="Старт", command=self.start_bot, width=12).pack(side="left", padx=4)
        tk.Button(row, text="Стоп", command=self.stop_bot, width=12).pack(side="left", padx=4)
        tk.Button(row, text="Выход", command=self.on_close, width=12).pack(side="left", padx=4)

        tk.Label(self.root, text="Лог:", font=("Arial", 10, "bold")).pack(anchor="w", padx=10)
        self.log_widget = tk.Text(self.root, height=14, wrap="word")
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


def print_usage() -> None:
    print("Использование:")
    print("  python assistant.py")
    print("  python assistant.py --list-voices")
    print('  python assistant.py --ask-screen "что на экране"')


if __name__ == "__main__":
    if "--list-voices" in sys.argv:
        list_voices()
        raise SystemExit(0)

    if "--ask-screen" in sys.argv:
        assistant = VoiceAssistant()
        idx = sys.argv.index("--ask-screen")
        if len(sys.argv) <= idx + 1:
            print_usage()
            raise SystemExit(1)
        question = sys.argv[idx + 1].strip()
        ok, msg = assistant.check_ollama()
        if not ok:
            print(msg)
            raise SystemExit(2)
        print(assistant.ask_about_screen(question))
        raise SystemExit(0)

    app = BotControlUI()
    app.run()
