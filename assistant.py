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

        if self.config.speech_backend == "sapi" and self._say_fallback_windows(text):
            return

        if self.tts is not None:
            try:
                self.tts.say(text)
                self.tts.runAndWait()
                return
            except Exception:
                self.tts = None

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

    # ---------- windows discovery/control ----------
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

            title_buf = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value.strip()
            if not title:
                return True

            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            process_name = ""
            exe_path = ""
            if psutil is not None:
                try:
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
            exe_name = self._normalize(Path(entry.get("exe", "")).name)
            hay = [title, process, exe_name]
            s = 0
            for field in hay:
                if not field:
                    continue
                if field == query_n:
                    s = max(s, 100)
                elif query_n in field:
                    s = max(s, 80)
                elif any(tok and tok in field for tok in query_n.split()):
                    s = max(s, 50)
            return s

        ranked = sorted(entries, key=score, reverse=True)
        return ranked[0] if ranked and score(ranked[0]) > 0 else None

    def _activate_hwnd_windows(self, hwnd: int) -> bool:
        if not self._is_windows:
            return False
        try:
            self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
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
            exe_n = self._normalize(Path(row.get("exe", "")).name)
            if query_n and (query_n in title_n or query_n in process_n or query_n in exe_n):
                if self._activate_hwnd_windows(row["window_id"]):
                    return True
        return False

    def close_window(self, title_part: str) -> bool:
        entry = self._find_best_window_entry(title_part)
        if not entry:
            return False
        hwnd = entry.get("window_id")
        if not hwnd:
            return False

        WM_CLOSE = 0x0010
        try:
            self.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return True
        except Exception:
            return False

    # ---------- app launching ----------
    def _desktop_shortcuts(self) -> list[Path]:
        paths = []
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            paths.append(Path(userprofile) / "Desktop")
        public = os.environ.get("PUBLIC", r"C:\Users\Public")
        paths.append(Path(public) / "Desktop")

        files = []
        for d in paths:
            if d.exists():
                files.extend(sorted(d.glob("*.lnk")))
                files.extend(sorted(d.glob("*.url")))
                files.extend(sorted(d.glob("*.exe")))
        return files

    def launch_application(self, app_name: str) -> tuple[bool, str]:
        app_name = app_name.strip()
        if not app_name:
            return False, "Не указано имя приложения"

        q = self._normalize(app_name)
        shortcuts = self._desktop_shortcuts()
        if not shortcuts:
            return False, "На рабочем столе не найдены ярлыки для запуска"

        best = None
        best_score = 0
        for item in shortcuts:
            stem_n = self._normalize(item.stem)
            score = 0
            if stem_n == q:
                score = 100
            elif q in stem_n:
                score = 80
            elif any(tok and tok in stem_n for tok in q.split()):
                score = 50
            if score > best_score:
                best = item
                best_score = score

        if best is None:
            return False, f"Не нашел приложение '{app_name}' на рабочем столе"

        try:
            os.startfile(str(best))  # type: ignore[attr-defined]
            return True, f"Запускаю {best.stem} с рабочего стола"
        except Exception as exc:
            return False, f"Не удалось запустить '{best.stem}': {exc}"

    # ---------- typing ----------
    def type_text(self, text: str) -> bool:
        if not text or pyautogui is None:
            return False

        # 1) paste from clipboard
        if pyperclip is not None:
            try:
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                return True
            except Exception:
                pass

        # 2) fallback direct typing
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
            else:
                # delete recent typed fragment quickly
                pyautogui.hotkey("ctrl", "shift", "left")
                pyautogui.press("backspace")
            return True
        except Exception:
            return False

    def send_message(self) -> None:
        if pyautogui is not None:
            pyautogui.press("enter")

    def press_hotkey(self, *keys: str) -> None:
        if pyautogui is not None:
            pyautogui.hotkey(*keys)

    # ---------- LM Studio ----------
    def _check_http_client(self) -> tuple[bool, str]:
        if requests is None:
            return False, "Модуль requests не установлен. Выполни: pip install -r requirements.txt"
        return True, "ok"

    def _lmstudio_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.lmstudio_api_key}",
            "Content-Type": "application/json",
        }

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
            "http://127.0.0.1:3000/v1",
            "http://localhost:3000/v1",
        ]
        uniq = []
        for u in raw:
            if u and u not in uniq:
                uniq.append(u)
        return uniq

    @staticmethod
    def _normalize_base_with_v1(base: str) -> str:
        b = base.rstrip("/")
        return b if b.endswith("/v1") else f"{b}/v1"

    def _try_lmstudio_models_request(self, base_v1: str) -> Optional[dict]:
        assert requests is not None
        url = base_v1.rstrip("/") + "/models"
        for headers in (self._lmstudio_headers(), {"Content-Type": "application/json"}):
            try:
                resp = requests.get(url, headers=headers, timeout=2.5)
                if resp.status_code >= 400:
                    continue
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("data", None), list):
                    return data
            except Exception:
                continue
        return None

    def _discover_lmstudio_base_url(self) -> tuple[bool, str]:
        ok, msg = self._check_http_client()
        if not ok:
            return False, msg

        if self._resolved_lmstudio_base_url:
            return True, self._resolved_lmstudio_base_url

        assert requests is not None
        for candidate in self._candidate_lmstudio_base_urls():
            normalized = self._normalize_base_with_v1(candidate)
            data = self._try_lmstudio_models_request(normalized)
            if data is not None:
                self._resolved_lmstudio_base_url = normalized
                self.log_cb(f"[INFO] LM Studio найден: {normalized}")
                return True, normalized

        return False, "Не удалось автоматически найти LM Studio. Включи Local Server в LM Studio"

    def _lmstudio_url(self, path: str) -> str:
        base = (self._resolved_lmstudio_base_url or self._normalize_base_with_v1(self.config.lmstudio_base_url)).rstrip("/")
        return base + path

    def check_lmstudio(self) -> tuple[bool, str]:
        found, discover_msg = self._discover_lmstudio_base_url()
        if not found:
            return False, discover_msg

        assert requests is not None
        try:
            resp = requests.get(self._lmstudio_url("/models"), headers=self._lmstudio_headers(), timeout=8)
            if resp.status_code >= 400:
                resp = requests.get(self._lmstudio_url("/models"), headers={"Content-Type": "application/json"}, timeout=8)
            resp.raise_for_status()
            models = resp.json().get("data", [])
            if not models:
                return False, "LM Studio найден, но нет загруженных моделей"
            return True, f"LM Studio готов ({self._resolved_lmstudio_base_url}). Моделей: {len(models)}"
        except Exception as exc:
            return False, f"LM Studio найден, но запрос не прошел: {exc}"

    def _get_active_lmstudio_model(self) -> Optional[str]:
        found, _ = self._discover_lmstudio_base_url()
        if not found or requests is None:
            return None

        resp = requests.get(self._lmstudio_url("/models"), headers=self._lmstudio_headers(), timeout=8)
        if resp.status_code >= 400:
            resp = requests.get(self._lmstudio_url("/models"), headers={"Content-Type": "application/json"}, timeout=8)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        if not models:
            return None
        return models[0].get("id")

    def ask_ai(self, question: str, image_b64: Optional[str] = None) -> str:
        try:
            model_id = self._get_active_lmstudio_model()
            if not model_id:
                return "В LM Studio нет активной модели"

            endpoint = self._lmstudio_url("/chat/completions")
            if image_b64:
                content = [
                    {
                        "type": "text",
                        "text": "Отвечай по-русски, кратко и по содержимому изображения. " f"Вопрос: {question}",
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ]
            else:
                content = "Ты помощник Вовчик. Отвечай по-русски, понятно и полезно. Вопрос: " + question

            payload = {"model": model_id, "messages": [{"role": "user", "content": content}], "temperature": 0.3}

            assert requests is not None
            resp = requests.post(endpoint, headers=self._lmstudio_headers(), json=payload, timeout=120)
            if resp.status_code >= 400:
                resp = requests.post(endpoint, headers={"Content-Type": "application/json"}, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            message = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(message, list):
                chunks = [p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") == "text"]
                message = "\n".join(chunks)
            answer = str(message).strip()
            return answer or "Модель не вернула ответ"
        except Exception as exc:
            return f"Ошибка обращения к LM Studio: {exc}"

    def ask_about_screen(self, question: str) -> str:
        if pyautogui is None:
            return "Модуль pyautogui не установлен"
        try:
            image = pyautogui.screenshot()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
            return self.ask_ai(question, image_b64=img_b64)
        except Exception as exc:
            return f"Ошибка при создании скриншота: {exc}"

    # ---------- intents ----------
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

        if any(w in c for w in ["выход", "стоп", "заверш", "закрой ассистента", "выключи бота"]):
            return "exit", ""
        if any(w in c for w in ["список окон", "какие окна", "открытые приложения", "список приложений"]):
            return "list_apps", ""
        if any(w in c for w in ["запусти", "стартуй", "открой приложение"]):
            return "launch_app", self._extract_text_after_keywords(c, ["запусти", "стартуй", "открой приложение"])
        if any(w in c for w in ["закрой окно", "закрой приложение"]):
            return "close_window", self._extract_text_after_keywords(c, ["закрой окно", "закрой приложение"])
        if any(w in c for w in ["стер", "удали текст", "очисти текст", "сотри"]):
            mode = "all" if "весь" in c or "полностью" in c else "line"
            return "erase_text", mode
        if any(w in c for w in ["отправ", "send", "вышли"]):
            return "send", ""
        if any(w in c for w in ["экран", "скрин", "что видишь", "анализ"]):
            q = self._extract_text_after_keywords(c, ["что на экране", "проанализируй экран", "посмотри экран", "видишь на экране", "экран"])
            return "screen", q or "Что находится на экране?"
        if any(w in c for w in ["переключ", "активируй", "открой окно", "сфокусируй"]):
            return "window", self._extract_text_after_keywords(c, ["открой окно", "переключись на", "активируй", "сфокусируй", "открой"])
        if any(w in c for w in ["напечат", "введи", "впиши", "набери"]):
            return "type", self._extract_text_after_keywords(c, ["напечатай", "введи", "впиши", "набери"])
        if any(w in c for w in ["нажми", "горяч", "комбинац"]):
            return "hotkey", self._extract_text_after_keywords(c, ["нажми", "комбинацию", "горячие клавиши"])
        return "ask_ai", c

    def handle_command(self, command: str) -> None:
        intent, value = self._parse_intent(command)

        if intent == "window":
            if value and self.activate_window(value):
                self.say(f"Окно {value} активировано")
            else:
                self.say("Не удалось активировать окно. Скажи точнее название")
            return

        if intent == "close_window":
            if value and self.close_window(value):
                self.say(f"Окно {value} закрыто")
            else:
                self.say("Не удалось закрыть выбранное окно")
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
                        f"[APP {idx}] title='{item.get('title','')}' process='{item.get('process','')}' exe='{Path(item.get('exe','')).name}' pid={item.get('pid')}"
                    )
            return

        if intent == "type":
            if not value:
                self.say("Не расслышал текст для ввода")
            else:
                self.say("Текст вставлен" if self.type_text(value) else "Не удалось ввести текст")
            return

        if intent == "erase_text":
            self.say("Текст удален" if self.delete_typed_text(value) else "Не удалось удалить текст")
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
            self.say(self.ask_about_screen(value))
            self._set_status("Готов к командам")
            return

        if intent == "ask_ai":
            ok, msg = self.check_lmstudio()
            if not ok:
                self.say(msg)
                return
            self._set_status("Думаю...")
            self.say(self.ask_ai(value))
            self._set_status("Готов к командам")
            return

        if intent == "exit":
            self.say("Останавливаюсь")
            self.stop()

    # ---------- runtime ----------
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
        self.root.geometry("840x520")
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

        hint = (
            "Команды: 'список открытых приложений', 'запусти <ярлык с рабочего стола>', "
            "'переключись на ...', 'закрой окно ...', 'напечатай ...', 'сотри текст', 'отправь'."
        )
        tk.Label(container, text=hint, bg=self.CARD, fg=self.SUB, wraplength=790, justify="left").pack(anchor="w", padx=14)

        tk.Label(container, text="Лог", bg=self.CARD, fg=self.SUB, font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 2))
        self.log_widget = tk.Text(
            container,
            height=20,
            wrap="word",
            bg="#0f1115",
            fg="#d1d5db",
            insertbackground="#d1d5db",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#2f3540",
            highlightcolor="#2f3540",
        )
        self.log_widget.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.assistant = VoiceAssistant(status_cb=self.set_status, log_cb=self.append_log)
        self.set_status("Готов. Нажми 'Старт' для запуска микрофона")
        missing = []
        if requests is None:
            missing.append("requests")
        if sr is None:
            missing.append("speech_recognition")
        if pyautogui is None:
            missing.append("pyautogui")
        if pyperclip is None:
            missing.append("pyperclip")
        if missing:
            self.append_log("[WARN] Не установлены модули: " + ", ".join(missing) + ". Выполни: pip install -r requirements.txt")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _btn(self, parent: tk.Widget, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.BTN,
            fg=self.FG,
            activebackground="#384152",
            activeforeground=self.FG,
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )

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
    for voice in tts.getProperty("voices"):
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

    ui = BotControlUI()
    ui.run()
