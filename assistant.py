import base64
import io
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

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
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()
        self.recognizer = sr.Recognizer()
        self.tts = pyttsx3.init()
        self._configure_voice()

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

    def say(self, text: str) -> None:
        print(f"[BOT] {text}")
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
    def _clean_phrase(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower()).strip()

    def activate_window(self, title_part: str) -> bool:
        if not title_part:
            return False

        # 1) Попытка через pygetwindow.
        try:
            windows = gw.getWindowsWithTitle(title_part)
            if windows:
                target = windows[0]
                if target.isMinimized:
                    target.restore()
                target.activate()
                return True
        except Exception:
            pass

        # 2) Linux fallback через wmctrl/xdotool.
        if shutil.which("wmctrl"):
            proc = subprocess.run(["wmctrl", "-a", title_part], capture_output=True, text=True)
            if proc.returncode == 0:
                return True

        if shutil.which("xdotool"):
            proc = subprocess.run(
                ["xdotool", "search", "--name", title_part, "windowactivate"],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                return True

        return False

    def type_text(self, text: str) -> None:
        if not text:
            return
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")

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

        prompt = (
            "Ты помощник Вовчик. У тебя есть изображение экрана пользователя. "
            "Отвечай только на русском языке, кратко и по делу. "
            f"Вопрос пользователя: {question}"
        )
        payload = {
            "model": self.config.vision_model,
            "prompt": prompt,
            "images": [img_b64],
            "stream": False,
        }

        try:
            resp = requests.post(self.config.ollama_url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            answer = data.get("response", "Не удалось получить ответ от модели").strip()
            if not answer:
                return "Модель не вернула текстовый ответ"
            return answer
        except Exception as exc:
            return f"Ошибка при анализе экрана: {exc}"

    @staticmethod
    def _extract_text_after_keywords(command: str, keywords: list[str]) -> str:
        for keyword in keywords:
            if keyword in command:
                suffix = command.split(keyword, 1)[1].strip(" .,!?")
                if suffix:
                    return suffix
        return ""

    def _parse_intent(self, command: str) -> tuple[str, str]:
        c = self._clean_phrase(command)

        if any(word in c for word in ["выход", "стоп", "заверш", "закрой ассистента"]):
            return "exit", ""

        if any(word in c for word in ["экран", "скрин", "что видишь", "анализ"]):
            question = self._extract_text_after_keywords(
                c,
                ["что на экране", "проанализируй экран", "посмотри экран", "видишь на экране", "экран"],
            )
            return "screen", question or "Что находится на экране?"

        if any(word in c for word in ["переключ", "активируй", "открой окно", "сфокусируй"]):
            title = self._extract_text_after_keywords(
                c,
                ["открой окно", "переключись на", "активируй", "сфокусируй", "открой"],
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
                self.say("Не нашел окно. Скажи точнее название приложения")
            return

        if intent == "type":
            if not value:
                self.say("Не расслышал текст для ввода")
                return
            self.type_text(value)
            self.say("Текст вставлен")
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
            self.say("Смотрю на экран")
            answer = self.ask_about_screen(value)
            self.say(answer)
            return

        if intent == "exit":
            self.say("Останавливаюсь")
            raise SystemExit(0)

        self.say("Пока не понял команду. Попробуй сказать иначе")

    def run(self) -> None:
        self.say("Голосовой ассистент запущен")
        while True:
            try:
                heard = self.listen()
                if not heard:
                    continue
                print(f"[YOU] {heard}")

                # Можно с wake-word и без него (вольные команды)
                if self.config.wake_word in heard:
                    heard = heard.replace(self.config.wake_word, "", 1).strip()
                    if not heard:
                        self.say("Слушаю")
                        heard = self.listen(timeout=6, phrase_time_limit=10)

                if heard:
                    print(f"[CMD] {heard}")
                    self.handle_command(heard)
            except sr.WaitTimeoutError:
                continue
            except KeyboardInterrupt:
                self.say("Завершение")
                break


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


if __name__ == "__main__":
    assistant = VoiceAssistant()

    if "--list-voices" in sys.argv:
        list_voices()
        raise SystemExit(0)

    if "--ask-screen" in sys.argv:
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

    assistant.run()
