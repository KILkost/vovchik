import base64
import io
import os
import sys
from dataclasses import dataclass
from typing import Optional

import pyautogui
import pygetwindow as gw
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
        """Выбор наиболее подходящего русского голоса."""
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
        self.tts.say(text)
        self.tts.runAndWait()

    def listen(self, timeout: int = 5, phrase_time_limit: int = 8) -> str:
        with sr.Microphone() as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)

        try:
            return self.recognizer.recognize_google(audio, language=self.config.language).lower()
        except sr.UnknownValueError:
            return ""
        except sr.RequestError:
            self.say("Не удалось обратиться к сервису распознавания.")
            return ""

    def activate_window(self, title_part: str) -> bool:
        windows = gw.getWindowsWithTitle(title_part)
        if not windows:
            return False
        target = windows[0]
        if target.isMinimized:
            target.restore()
        target.activate()
        return True

    def type_text(self, text: str) -> None:
        pyautogui.write(text, interval=0.03)

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
            "prompt": f"Ответь кратко и по-русски. Вопрос пользователя: {question}",
            "images": [img_b64],
            "stream": False,
        }

        try:
            resp = requests.post(self.config.ollama_url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "Не удалось получить ответ от модели.").strip()
        except Exception as exc:
            return f"Ошибка при анализе экрана: {exc}"

    @staticmethod
    def _extract_screen_question(command: str) -> str:
        prefixes = ("что на экране", "проанализируй экран")
        for prefix in prefixes:
            if command.startswith(prefix):
                q = command[len(prefix):].strip(" .,!?")
                return q if q else "Что находится на экране?"
        return "Что находится на экране?"

    def handle_command(self, command: str) -> None:
        if command.startswith("открой окно"):
            title = command.replace("открой окно", "", 1).strip()
            if title and self.activate_window(title):
                self.say(f"Окно {title} активировано")
            else:
                self.say("Не нашел такое окно")
            return

        if command.startswith("напечатай"):
            text = command.replace("напечатай", "", 1).strip()
            self.type_text(text)
            self.say("Готово")
            return

        if command.startswith("нажми"):
            raw = command.replace("нажми", "", 1).strip()
            keys = tuple(part.strip() for part in raw.split("+") if part.strip())
            if keys:
                self.press_hotkey(*keys)
                self.say("Сделано")
            else:
                self.say("Не понял комбинацию клавиш")
            return

        if command.startswith("что на экране") or command.startswith("проанализируй экран"):
            ok, msg = self.check_ollama()
            if not ok:
                self.say(msg)
                return
            question = self._extract_screen_question(command)
            self.say("Анализирую экран")
            answer = self.ask_about_screen(question)
            self.say(answer)
            return

        if command in {"выход", "стоп", "завершить"}:
            self.say("Останавливаюсь")
            raise SystemExit(0)

        self.say("Команда не распознана")

    def run(self) -> None:
        self.say("Голосовой ассистент запущен")
        while True:
            try:
                heard = self.listen()
                if not heard:
                    continue
                print(f"[YOU] {heard}")
                if self.config.wake_word in heard:
                    self.say("Слушаю")
                    command = self.listen(timeout=6, phrase_time_limit=10)
                    if command:
                        print(f"[CMD] {command}")
                        self.handle_command(command)
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
