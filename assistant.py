import base64
import ctypes
import io
import os
import platform
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
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


@dataclass
class Config:
    wake_word: str = "ассистент"
    language: str = "ru-RU"
    lmstudio_base_url: str = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    lmstudio_api_key: str = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    speech_backend: str = os.getenv("SPEECH_BACKEND", "sapi")


class VoiceAssistant:
    def __init__(self, config: Optional[Config] = None, status_cb: Optional[Callable[[str], None]] = None, log_cb: Optional[Callable[[str], None]] = None) -> None:
        self.config = config or Config()
        self.status_cb = status_cb or (lambda _m: None)
        self.log_cb = log_cb or (lambda _m: None)

        self.mode = "commands"  # commands | dialogue
        self._last_typed_text = ""

        self._is_windows = platform.system() == "Windows"
        self.user32 = ctypes.windll.user32 if self._is_windows else None
        self._resolved_lmstudio_base_url: Optional[str] = None

        self.recognizer = sr.Recognizer() if sr else None
        self.tts = None
        self._init_tts()

        self._run_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ---------- speech ----------
    def _init_tts(self) -> None:
        if self.config.speech_backend == "sapi" or pyttsx3 is None:
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
        for voice in voices:
            meta = f"{voice.name} {voice.id}".lower()
            if "ru" in meta or "russian" in meta or "рус" in meta:
                self.tts.setProperty("voice", voice.id)
                break
        self.tts.setProperty("rate", 170)
        self.tts.setProperty("volume", 1.0)

    def _say_fallback_windows(self, text: str) -> bool:
        if not self._is_windows:
            return False
        ps_script = (
            "Add-Type -AssemblyName System.Speech;"
            "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$speak.Speak([Console]::In.ReadToEnd())"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            return True
        except Exception:
            return False

    def say(self, text: str) -> None:
        self.log_cb(f"[BOT] {text}")

        if self.config.speech_backend == "sapi" and self._say_fallback_windows(text):
            return

        if self.tts is not None:
            try:
                self.tts.say(text)
                self.tts.runAndWait()
                return
            except Exception:
                self.tts = None

        if self.config.speech_backend != "sapi" and pyttsx3 is not None:
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
        if not sr or not self.recognizer:
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

    # ---------- helpers ----------
    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[^\wа-яё]+", "", text.casefold(), flags=re.IGNORECASE)

    def _set_status(self, text: str) -> None:
        self.status_cb(text)
        self.log_cb(f"[STATUS] {text}")

    # ---------- windows ----------
    def _enumerate_windows_windows(self) -> list[dict]:
        if not self._is_windows:
            return []

        rows: list[dict] = []
        cbtype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            title_buf = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value.strip()
            if not title:
                return True

            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            process_name, exe_path = "", ""
            if psutil is not None:
                try:
                    proc = psutil.Process(pid.value)
                    process_name = proc.name()
                    exe_path = proc.exe()
                except Exception:
                    pass

            rows.append({"window_id": int(hwnd), "pid": int(pid.value), "process": process_name, "exe": exe_path, "title": title})
            return True

        self.user32.EnumWindows(cbtype(callback), 0)
        return rows

    def list_open_apps_detailed(self) -> list[dict]:
        return self._enumerate_windows_windows()

    def _find_best_window_entry(self, query: str) -> Optional[dict]:
        q = self._normalize(query)
        if not q:
            return None

        entries = self.list_open_apps_detailed()
        if not entries:
            return None

        def score(e: dict) -> int:
            fields = [self._normalize(e.get("title", "")), self._normalize(e.get("process", "")), self._normalize(Path(e.get("exe", "")).name)]
            s = 0
            for f in fields:
                if not f:
                    continue
                if f == q:
                    s = max(s, 100)
                elif q in f:
                    s = max(s, 80)
                elif any(tok and tok in f for tok in q.split()):
                    s = max(s, 50)
            return s

        ranked = sorted(entries, key=score, reverse=True)
        return ranked[0] if ranked and score(ranked[0]) > 0 else None

    def _activate_hwnd_windows(self, hwnd: int) -> bool:
        if not self._is_windows:
            return False
        try:
            self.user32.ShowWindow(hwnd, 9)
            return bool(self.user32.SetForegroundWindow(hwnd))
        except Exception:
            return False

    def activate_window(self, title_part: str) -> bool:
        entry = self._find_best_window_entry(title_part)
        return bool(entry and entry.get("window_id") and self._activate_hwnd_windows(entry["window_id"]))

    def close_window(self, title_part: str) -> bool:
        entry = self._find_best_window_entry(title_part)
        if not entry or not entry.get("window_id"):
            return False
        try:
            self.user32.PostMessageW(entry["window_id"], 0x0010, 0, 0)
            return True
        except Exception:
            return False

    # ---------- desktop launch ----------
    def _desktop_shortcuts(self) -> list[Path]:
        paths = []
        if os.environ.get("USERPROFILE"):
            paths.append(Path(os.environ["USERPROFILE"]) / "Desktop")
        paths.append(Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop")

        files: list[Path] = []
        for d in paths:
            if d.exists():
                files += sorted(d.glob("*.lnk")) + sorted(d.glob("*.url")) + sorted(d.glob("*.exe"))
        return files

    def launch_application(self, app_name: str) -> tuple[bool, str]:
        app_name = app_name.strip()
        if not app_name:
            return False, "Не указано имя приложения"

        q = self._normalize(app_name)
        options = self._desktop_shortcuts()
        if not options:
            return False, "На рабочем столе не найдены ярлыки/программы"

        best, best_score = None, 0
        for f in options:
            stem = self._normalize(f.stem)
            score = 100 if stem == q else 80 if q in stem else 50 if any(tok and tok in stem for tok in q.split()) else 0
            if score > best_score:
                best, best_score = f, score

        if best is None:
            return False, f"Не нашел '{app_name}' на рабочем столе"

        try:
            os.startfile(str(best))  # type: ignore[attr-defined]
            return True, f"Запускаю {best.stem}"
        except Exception as exc:
            return False, f"Не удалось запустить {best.stem}: {exc}"

    # ---------- typing ----------
    def type_text(self, text: str) -> bool:
        if not text or pyautogui is None:
            return False

        self._last_typed_text = text

        if pyperclip is not None:
            try:
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                return True
            except Exception:
                pass
            try:
                pyautogui.hotkey("shift", "insert")
                return True
            except Exception:
                pass

        try:
            pyautogui.write(text, interval=0.01)
            return True
        except Exception:
            return False

    def delete_typed_text(self, mode: str = "line") -> bool:
        if pyautogui is None:
            return False
        try:
            if mode == "all":
                pyautogui.hotkey("ctrl", "a")
                pyautogui.press("backspace")
                self._last_typed_text = ""
                return True

            # line mode: first try word-select delete
            try:
                pyautogui.hotkey("ctrl", "shift", "left")
                pyautogui.press("backspace")
                self._last_typed_text = ""
                return True
            except Exception:
                pass

            # robust fallback: delete by length of last typed text
            if self._last_typed_text:
                for _ in range(len(self._last_typed_text)):
                    pyautogui.press("backspace")
                self._last_typed_text = ""
                return True
            return False
        except Exception:
            return False

    def send_message(self) -> None:
        if pyautogui is not None:
            pyautogui.press("enter")

    # ---------- LM Studio ----------
    def _check_http_client(self) -> tuple[bool, str]:
        if requests is None:
            return False, "Модуль requests не установлен. Выполни: pip install -r requirements.txt"
        return True, "ok"

    def _lmstudio_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.lmstudio_api_key}", "Content-Type": "application/json"}

    def _candidate_lmstudio_base_urls(self) -> list[str]:
        configured = self.config.lmstudio_base_url.rstrip("/")
        raw = [
            configured,
            configured.replace("/v1", ""),
            "http://127.0.0.1:1234/v1",
            "http://localhost:1234/v1",
            "http://127.0.0.1:1234",
            "http://localhost:1234",
            "http://127.0.0.1:8080/v1",
            "http://localhost:8080/v1",
        ]
        return list(dict.fromkeys([u for u in raw if u]))

    @staticmethod
    def _normalize_base_with_v1(base: str) -> str:
        b = base.rstrip("/")
        return b if b.endswith("/v1") else f"{b}/v1"

    def _try_models_request(self, base_v1: str) -> bool:
        assert requests is not None
        url = base_v1 + "/models"
        for headers in (self._lmstudio_headers(), {"Content-Type": "application/json"}):
            try:
                resp = requests.get(url, headers=headers, timeout=2.5)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    return True
            except Exception:
                continue
        return False

    def _discover_lmstudio_base_url(self) -> tuple[bool, str]:
        ok, msg = self._check_http_client()
        if not ok:
            return False, msg

        if self._resolved_lmstudio_base_url:
            return True, self._resolved_lmstudio_base_url

        assert requests is not None
        for c in self._candidate_lmstudio_base_urls():
            n = self._normalize_base_with_v1(c)
            if self._try_models_request(n):
                self._resolved_lmstudio_base_url = n
                self.log_cb(f"[INFO] LM Studio найден: {n}")
                return True, n

        return False, "Не удалось найти LM Studio. Включи Local Server"

    def _lmstudio_url(self, path: str) -> str:
        base = (self._resolved_lmstudio_base_url or self._normalize_base_with_v1(self.config.lmstudio_base_url)).rstrip("/")
        return base + path

    def check_lmstudio(self) -> tuple[bool, str]:
        found, msg = self._discover_lmstudio_base_url()
        if not found:
            return False, msg
        try:
            assert requests is not None
            resp = requests.get(self._lmstudio_url("/models"), headers=self._lmstudio_headers(), timeout=8)
            if resp.status_code >= 400:
                resp = requests.get(self._lmstudio_url("/models"), headers={"Content-Type": "application/json"}, timeout=8)
            resp.raise_for_status()
            models = resp.json().get("data", [])
            if not models:
                return False, "LM Studio найден, но нет загруженных моделей"
            return True, f"LM Studio готов ({self._resolved_lmstudio_base_url}). Моделей: {len(models)}"
        except Exception as exc:
            return False, f"Ошибка доступа к LM Studio: {exc}"

    def _get_active_model(self) -> Optional[str]:
        found, _ = self._discover_lmstudio_base_url()
        if not found or requests is None:
            return None
        resp = requests.get(self._lmstudio_url("/models"), headers=self._lmstudio_headers(), timeout=8)
        if resp.status_code >= 400:
            resp = requests.get(self._lmstudio_url("/models"), headers={"Content-Type": "application/json"}, timeout=8)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[0].get("id") if data else None

    def _extract_last_answer(self, text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return "Ответ в LM Studio не найден"
        return lines[-1]

    def ask_ai_via_lmstudio_window(self, question: str) -> str:
        if pyautogui is None or pyperclip is None:
            return "Для ручного режима нужен pyautogui и pyperclip"

        if not self.activate_window("lm studio"):
            return "Не удалось активировать окно LM Studio"

        try:
            time.sleep(0.35)
            pyperclip.copy(question)
            pyautogui.hotkey("ctrl", "v")
            pyautogui.press("enter")
            self.log_cb("[INFO] Запрос отправлен в окно LM Studio")

            # Попытка автоматически прочитать ответ из выделенного текста в окне
            time.sleep(5.0)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.1)
            return self._extract_last_answer(pyperclip.paste())
        except Exception as exc:
            return f"Ручной режим LM Studio не удался: {exc}"

    def ask_ai_api(self, question: str, image_b64: Optional[str] = None) -> str:
        try:
            model = self._get_active_model()
            if not model:
                return "В LM Studio нет активной модели"

            content = (
                [
                    {"type": "text", "text": f"Отвечай по-русски, кратко. Вопрос: {question}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ]
                if image_b64
                else f"Ты помощник Вовчик. Отвечай по-русски, полезно и кратко. Вопрос: {question}"
            )
            payload = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.3}

            assert requests is not None
            endpoint = self._lmstudio_url("/chat/completions")
            resp = requests.post(endpoint, headers=self._lmstudio_headers(), json=payload, timeout=120)
            if resp.status_code >= 400:
                resp = requests.post(endpoint, headers={"Content-Type": "application/json"}, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(message, list):
                message = "\n".join([p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") == "text"])
            return str(message).strip() or "Модель не вернула ответ"
        except Exception as exc:
            return f"Ошибка API LM Studio: {exc}"

    def ask_about_screen(self, question: str) -> str:
        if pyautogui is None:
            return "Модуль pyautogui не установлен"
        try:
            img = pyautogui.screenshot()
            buff = io.BytesIO()
            img.save(buff, format="PNG")
            return self.ask_ai_api(question, base64.b64encode(buff.getvalue()).decode("utf-8"))
        except Exception as exc:
            return f"Ошибка скриншота: {exc}"

    # ---------- intent parsing ----------
    @staticmethod
    def _clean_phrase(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower()).strip()

    @staticmethod
    def _extract_text_after_keywords(command: str, keywords: list[str]) -> str:
        for kw in keywords:
            if kw in command:
                suffix = command.split(kw, 1)[1].strip(" .,!?:;-")
                if suffix:
                    return suffix
        return ""

    def _parse_intent(self, command: str) -> tuple[str, str]:
        c = self._clean_phrase(command)

        if "режим диалога" in c:
            return "mode_dialogue", ""
        if "режим команд" in c:
            return "mode_commands", ""

        if any(w in c for w in ["выход", "стоп", "заверш", "закрой ассистента", "выключи бота"]):
            return "exit", ""
        if any(w in c for w in ["список окон", "какие окна", "открытые приложения", "открытых приложений", "список приложений"]):
            return "list_apps", ""
        if any(w in c for w in ["запусти", "стартуй", "открой приложение"]):
            return "launch_app", self._extract_text_after_keywords(c, ["запусти", "стартуй", "открой приложение"])
        if any(w in c for w in ["закрой окно", "закрой приложение"]):
            return "close_window", self._extract_text_after_keywords(c, ["закрой окно", "закрой приложение"])
        if any(w in c for w in ["стер", "стереть", "удали текст", "очисти текст", "сотри", "сотри весь текст", "удали весь текст", "очисти всё"]):
            return "erase_text", "all" if ("весь" in c or "все" in c or "всё" in c or "полностью" in c) else "line"
        if any(w in c for w in ["отправ", "send", "вышли"]):
            return "send", ""
        if any(w in c for w in ["экран", "скрин", "что видишь", "анализ"]):
            q = self._extract_text_after_keywords(c, ["что на экране", "проанализируй экран", "посмотри экран", "видишь на экране", "экран"])
            return "screen", q or "Что находится на экране?"
        if any(w in c for w in ["переключ", "активируй", "открой окно", "сфокусируй"]):
            return "window", self._extract_text_after_keywords(c, ["открой окно", "переключись на", "активируй", "сфокусируй", "открой"])
        if any(w in c for w in ["напечат", "введи", "впиши", "набери", "напиши", "печатай"]):
            return "type", self._extract_text_after_keywords(c, ["напечатай", "введи", "впиши", "набери", "напиши", "печатай"])
        if any(w in c for w in ["нажми", "горяч", "комбинац"]):
            return "hotkey", self._extract_text_after_keywords(c, ["нажми", "комбинацию", "горячие клавиши"])
        return "ask_ai", c

    def handle_command(self, command: str) -> None:
        intent, value = self._parse_intent(command)

        if intent == "mode_dialogue":
            self.mode = "dialogue"
            self.say("Включил режим диалога")
            self._set_status("Режим: диалог")
            return

        if intent == "mode_commands":
            self.mode = "commands"
            self.say("Включил режим команд")
            self._set_status("Режим: команды")
            return

        if intent == "window":
            self.say(f"Окно {value} активировано" if value and self.activate_window(value) else "Не удалось активировать окно")
            return

        if intent == "close_window":
            self.say(f"Окно {value} закрыто" if value and self.close_window(value) else "Не удалось закрыть выбранное окно")
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
                for i, item in enumerate(apps, 1):
                    self.log_cb(f"[APP {i}] title='{item.get('title','')}' process='{item.get('process','')}' exe='{Path(item.get('exe','')).name}' pid={item.get('pid')}")
            return

        if intent == "type":
            self.say("Текст вставлен" if value and self.type_text(value) else "Не удалось ввести текст")
            return

        if intent == "erase_text":
            self.say("Текст удален" if self.delete_typed_text(value) else "Не удалось удалить текст")
            return

        if intent == "send":
            self.send_message()
            self.say("Отправил")
            return

        if intent == "hotkey":
            if pyautogui is None:
                self.say("pyautogui не установлен")
                return
            keys = tuple(p.strip() for p in value.split("+") if p.strip())
            if keys:
                pyautogui.hotkey(*keys)
                self.say("Сделано")
            else:
                self.say("Не понял комбинацию")
            return

        if intent == "screen":
            ok, msg = self.check_lmstudio()
            if not ok:
                self.say(msg)
                return
            self._set_status("Анализирую экран...")
            self.say(self.ask_about_screen(value))
            self._set_status("Готов")
            return

        if intent == "ask_ai":
            if self.mode != "dialogue":
                self.say("Сейчас режим команд. Скажи: режим диалога")
                return

            # В режиме диалога сперва пробуем системный API путь LM Studio.
            self._set_status("Режим диалога: API запрос")
            ok, msg = self.check_lmstudio()
            if ok:
                answer = self.ask_ai_api(value)
                if answer.startswith("Ошибка API") or answer.startswith("В LM Studio нет"):
                    self._set_status("API не сработал, пробую ручной режим")
                    manual = self.ask_ai_via_lmstudio_window(value)
                    answer = manual if not manual.startswith("Не удалось") else f"{answer}. {manual}"
            else:
                self._set_status("API недоступен, пробую ручной режим")
                answer = self.ask_ai_via_lmstudio_window(value)
                if answer.startswith("Не удалось"):
                    answer = f"Нет API подключения: {msg}. {answer}"

            self.say(answer)
            self._set_status("Готов")
            return

        if intent == "exit":
            self.say("Останавливаюсь")
            self.stop()

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
    BG = "#14161a"
    CARD = "#1d2128"
    FG = "#e5e7eb"
    SUB = "#9ca3af"
    ACCENT = "#3b82f6"
    BTN = "#2b313b"

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Вовчик — статус")
        self.root.geometry("860x540")
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", True)

        self.status_var = tk.StringVar(value="Загрузка...")

        container = tk.Frame(self.root, bg=self.CARD, bd=0, highlightthickness=0)
        container.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(container, text="ВОВЧИК", bg=self.CARD, fg=self.FG, font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(container, text="Статус", bg=self.CARD, fg=self.SUB, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 0))
        tk.Label(container, textvariable=self.status_var, bg=self.CARD, fg=self.ACCENT, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14)

        row = tk.Frame(container, bg=self.CARD)
        row.pack(fill="x", padx=10, pady=12)
        self._btn(row, "Старт", self.start_bot).pack(side="left", padx=4)
        self._btn(row, "Стоп", self.stop_bot).pack(side="left", padx=4)
        self._btn(row, "Выход", self.on_close).pack(side="left", padx=4)

        hint = "Команды: режим диалога/режим команд, запусти <ярлык>, закрой окно <имя>, напечатай, сотри текст, отправь."
        tk.Label(container, text=hint, bg=self.CARD, fg=self.SUB, wraplength=810, justify="left").pack(anchor="w", padx=14)

        tk.Label(container, text="Лог", bg=self.CARD, fg=self.SUB, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 2))
        self.log_widget = tk.Text(container, height=20, wrap="word", bg="#0f1115", fg="#d1d5db", insertbackground="#d1d5db", relief="flat", bd=0, highlightthickness=1, highlightbackground="#2f3540")
        self.log_widget.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.assistant = VoiceAssistant(status_cb=self.set_status, log_cb=self.append_log)
        self.set_status("Готов. Нажми 'Старт'")

        missing = []
        if requests is None:
            missing.append("requests")
        if sr is None:
            missing.append("speech_recognition")
        if pyautogui is None:
            missing.append("pyautogui")
        if missing:
            self.append_log("[WARN] Не установлены модули: " + ", ".join(missing) + ". Выполни: pip install -r requirements.txt")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _btn(self, parent: tk.Widget, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=self.BTN, fg=self.FG, activebackground="#384152", activeforeground=self.FG, relief="flat", bd=0, padx=14, pady=8, font=("Segoe UI", 10, "bold"), cursor="hand2")

    def set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def append_log(self, text: str) -> None:
        self.root.after(0, lambda: (self.log_widget.insert("end", text + "\n"), self.log_widget.see("end")))

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
    for v in tts.getProperty("voices"):
        print(f"id={v.id} | name={v.name}")


def print_usage() -> None:
    print("Использование:")
    print("  python assistant.py")
    print("  python assistant.py --list-voices")
    print('  python assistant.py --ask "любой вопрос"')
    print('  python assistant.py --ask-screen "что на экране"')


def parse_cli_value(flag: str) -> Optional[str]:
    if flag not in sys.argv:
        return None
    i = sys.argv.index(flag)
    return sys.argv[i + 1].strip() if len(sys.argv) > i + 1 else None


if __name__ == "__main__":
    if "--list-voices" in sys.argv:
        list_voices()
        raise SystemExit(0)

    if "--ask" in sys.argv:
        q = parse_cli_value("--ask")
        if not q:
            print_usage()
            raise SystemExit(1)
        a = VoiceAssistant()
        ok, msg = a.check_lmstudio()
        if not ok:
            print(msg)
            raise SystemExit(2)
        print(a.ask_ai_api(q))
        raise SystemExit(0)

    if "--ask-screen" in sys.argv:
        q = parse_cli_value("--ask-screen")
        if not q:
            print_usage()
            raise SystemExit(1)
        a = VoiceAssistant()
        ok, msg = a.check_lmstudio()
        if not ok:
            print(msg)
            raise SystemExit(2)
        print(a.ask_about_screen(q))
        raise SystemExit(0)

    BotControlUI().run()
