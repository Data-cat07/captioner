import base64
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import soundcard as sc
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
import websocket


OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
RESPONSES_URL = "https://api.openai.com/v1/responses"
INPUT_RATE = 48000
OPENAI_RATE = 24000
CHUNK_SECONDS = 0.25
COMMIT_SECONDS = 6.0

TERM_REPLACEMENTS = {
    "매일 메이커 케이스": "메이커케이스",
    "메이커 케이스": "메이커케이스",
    "메이커 케이스는": "메이커케이스는",
    "레이저 커팅기 도면 자동으로": "레이저 커팅기 도면을 자동으로",
    "들어주는 사이트": "만들어주는 사이트",
}


LANGUAGES = {
    "한국어만": None,
    "English": "English",
    "Vietnamese": "Vietnamese",
    "Chinese": "Chinese",
    "Japanese": "Japanese",
    "Russian": "Russian",
    "Thai": "Thai",
    "Mongolian": "Mongolian",
    "Uzbek": "Uzbek",
    "Arabic": "Arabic",
}

TRANSCRIPTION_MODELS = [
    "gpt-realtime-whisper",
]


@dataclass
class CaptionEvent:
    kind: str
    text: str


def pcm16_base64(samples: np.ndarray) -> str:
    if samples.ndim > 1:
        samples = samples.mean(axis=1)

    samples = np.nan_to_num(samples, copy=False)
    samples = np.clip(samples, -1.0, 1.0)

    if INPUT_RATE != OPENAI_RATE:
        old_x = np.linspace(0, 1, num=samples.shape[0], endpoint=False)
        new_len = max(1, int(samples.shape[0] * OPENAI_RATE / INPUT_RATE))
        new_x = np.linspace(0, 1, num=new_len, endpoint=False)
        samples = np.interp(new_x, old_x, samples).astype(np.float32)

    pcm = (samples * 32767).astype(np.int16)
    return base64.b64encode(pcm.tobytes()).decode("ascii")


def normalize_caption_text(text: str) -> str:
    normalized = text
    for source, target in TERM_REPLACEMENTS.items():
        normalized = normalized.replace(source, target)
    return normalized


class Translator:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = os.environ.get("OPENAI_TRANSLATION_MODEL", "gpt-5.4-mini")

    def translate(self, text: str, target_language: Optional[str]) -> str:
        if not target_language or not text.strip():
            return ""

        prompt = (
            "Translate the Korean classroom caption into the target language. "
            "Keep it short, age-appropriate for elementary students, and do not add explanations.\n\n"
            f"Target language: {target_language}\n"
            f"Korean: {text}"
        )
        response = requests.post(
            RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": prompt,
                "reasoning": {"effort": "none"},
                "max_output_tokens": 300,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if "output_text" in data:
            return data["output_text"].strip()

        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))
        return "".join(chunks).strip()


class RealtimeCaptioner:
    def __init__(
        self,
        api_key: str,
        events: "queue.Queue[CaptionEvent]",
        transcription_model: str,
    ):
        self.api_key = api_key
        self.events = events
        self.transcription_model = transcription_model
        self.stop_event = threading.Event()
        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_ready = threading.Event()
        self.audio_thread: Optional[threading.Thread] = None
        self.socket_thread: Optional[threading.Thread] = None
        self.partial_text = ""
        self.audio_started = False

    def start(self) -> None:
        self.stop_event.clear()
        self.ws_ready.clear()
        self.socket_thread = threading.Thread(target=self._run_socket, daemon=True)
        self.socket_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _run_socket(self) -> None:
        headers = [
            f"Authorization: Bearer {self.api_key}",
            "OpenAI-Safety-Identifier: metaverse-captioner-local",
        ]
        self.ws = websocket.WebSocketApp(
            OPENAI_REALTIME_URL,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        transcription = {
            "model": self.transcription_model,
            "language": "ko",
        }
        if self.transcription_model == "gpt-realtime-whisper":
            transcription["delay"] = "low"

        config = {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                        "noise_reduction": None,
                        "transcription": transcription,
                    }
                },
            },
        }
        ws.send(json.dumps(config))
        self.ws_ready.set()
        self.events.put(CaptionEvent("status", "Connected. Waiting for session setup..."))

    def _on_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            return

        event_type = event.get("type")
        if event_type in {"session.created", "session.updated"}:
            self.events.put(CaptionEvent("status", "Session ready. Capturing system audio..."))
            if not self.audio_started:
                self.audio_started = True
                self.audio_thread = threading.Thread(target=self._capture_audio, daemon=True)
                self.audio_thread.start()
        elif event_type == "conversation.item.input_audio_transcription.delta":
            self.partial_text += event.get("delta", "")
            self.events.put(CaptionEvent("partial", self.partial_text))
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "").strip()
            self.partial_text = ""
            if transcript:
                self.events.put(CaptionEvent("final", transcript))
        elif event_type == "error":
            error = event.get("error", {})
            self.events.put(CaptionEvent("error", error.get("message", str(error))))
            self.stop_event.set()

    def _on_error(self, _ws: websocket.WebSocketApp, error: Exception) -> None:
        self.events.put(CaptionEvent("error", str(error)))

    def _on_close(self, *_args) -> None:
        self.stop_event.set()
        self.events.put(CaptionEvent("status", "Connection closed. Check API key, model access, or network."))

    def _send(self, payload: dict) -> bool:
        if self.stop_event.is_set() or not self.ws:
            return False
        sock = getattr(self.ws, "sock", None)
        if not sock or not getattr(sock, "connected", False):
            self.stop_event.set()
            return False
        try:
            self.ws.send(json.dumps(payload))
            return True
        except websocket.WebSocketConnectionClosedException:
            self.stop_event.set()
            self.events.put(CaptionEvent("status", "Connection closed while sending audio."))
            return False

    def _capture_audio(self) -> None:
        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(speaker.name, include_loopback=True)
            chunk_frames = int(INPUT_RATE * CHUNK_SECONDS)
            commit_chunks = max(1, int(COMMIT_SECONDS / CHUNK_SECONDS))
            sent_chunks = 0

            with loopback.recorder(samplerate=INPUT_RATE, channels=2) as recorder:
                while not self.stop_event.is_set():
                    data = recorder.record(numframes=chunk_frames)
                    audio = pcm16_base64(data)
                    if not self._send({"type": "input_audio_buffer.append", "audio": audio}):
                        break
                    sent_chunks += 1
                    if sent_chunks >= commit_chunks:
                        if not self._send({"type": "input_audio_buffer.commit"}):
                            break
                        sent_chunks = 0
        except Exception as exc:
            if not self.stop_event.is_set():
                self.events.put(CaptionEvent("error", f"Audio capture failed: {exc}"))
            self.stop()


class CaptionWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Metaverse Captioner")
        self.root.configure(bg="#111111")
        self.root.attributes("-topmost", True)
        self.root.minsize(720, 150)

        self.events: "queue.Queue[CaptionEvent]" = queue.Queue()
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.captioner: Optional[RealtimeCaptioner] = None
        self.translator = Translator(self.api_key) if self.api_key else None
        self.final_lines: list[str] = []
        self.display_text = ""
        self.log_lines: list[str] = []

        self.language_var = tk.StringVar(value="한국어만")
        self.font_size_var = tk.IntVar(value=24)
        self.opacity_var = tk.DoubleVar(value=0.92)
        self.width_var = tk.IntVar(value=86)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self._place_bottom()
        self._poll_events()

    def _build_ui(self) -> None:
        control = tk.Frame(self.root, bg="#191919")
        control.pack(fill="x")

        self.start_btn = tk.Button(control, text="Start", command=self.start, width=8)
        self.start_btn.pack(side="left", padx=6, pady=6)

        self.stop_btn = tk.Button(control, text="Stop", command=self.stop, width=8, state="disabled")
        self.stop_btn.pack(side="left", padx=3, pady=6)

        tk.Button(control, text="API Key", command=self.ask_api_key, width=8).pack(side="left", padx=3, pady=6)

        ttk.Label(control, text="Language").pack(side="left", padx=(12, 4))
        ttk.Combobox(
            control,
            textvariable=self.language_var,
            values=list(LANGUAGES.keys()),
            state="readonly",
            width=14,
        ).pack(side="left")

        ttk.Label(control, text="Font").pack(side="left", padx=(12, 4))
        tk.Spinbox(control, from_=16, to=48, textvariable=self.font_size_var, width=4, command=self._update_style).pack(
            side="left"
        )

        ttk.Label(control, text="Opacity").pack(side="left", padx=(12, 4))
        tk.Scale(
            control,
            from_=0.45,
            to=1.0,
            resolution=0.05,
            orient="horizontal",
            variable=self.opacity_var,
            command=lambda _v: self.root.attributes("-alpha", self.opacity_var.get()),
            length=100,
            bg="#191919",
            fg="#ffffff",
            highlightthickness=0,
        ).pack(side="left")

        tk.Button(control, text="Save log", command=self.save_log).pack(side="right", padx=6, pady=6)

        self.caption = tk.Text(
            self.root,
            bg="#050505",
            fg="#ffffff",
            padx=16,
            pady=14,
            wrap="word",
            height=4,
            relief="flat",
            insertwidth=0,
            state="disabled",
        )
        self.caption.pack(fill="both", expand=True)
        self._show_caption("수업 소리가 나오면 Start를 눌러 자막을 시작하세요.", append=False)

        status = tk.Label(self.root, textvariable=self.status_var, bg="#111111", fg="#bbbbbb", anchor="w", padx=8)
        status.pack(fill="x")
        self._update_style()

    def _place_bottom(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = int(screen_w * 0.78)
        height = 190
        x = int((screen_w - width) / 2)
        y = screen_h - height - 48
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.attributes("-alpha", self.opacity_var.get())

    def _update_style(self) -> None:
        size = self.font_size_var.get()
        self.caption.configure(font=("Malgun Gothic", size, "bold"))

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(80, self._poll_events)

    def _handle_event(self, event: CaptionEvent) -> None:
        if event.kind == "partial":
            if not LANGUAGES.get(self.language_var.get()):
                self._show_live_partial(event.text)
        elif event.kind == "final":
            text = normalize_caption_text(event.text)
            self.final_lines.append(text)
            self.log_lines.append(f"[ko] {text}")
            if LANGUAGES.get(self.language_var.get()):
                self.status_var.set("Translating...")
                self._translate_async(text)
            else:
                self._show_caption(text)
        elif event.kind == "translation":
            self.log_lines.append(f"[{self.language_var.get()}] {event.text}")
            self._show_caption(event.text)
            self.status_var.set("Captioning...")
        elif event.kind == "status":
            self.status_var.set(event.text)
        elif event.kind == "error":
            self.status_var.set(event.text)
            self._show_caption(f"오류: {event.text}")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if "invalid_api_key" in event.text or "Incorrect API key" in event.text:
                self.api_key = ""
                self.translator = None
                self.root.after(100, self.ask_api_key)
            elif "invalid_model" in event.text:
                self.status_var.set("Model rejected. Check model access for realtime transcription or translation.")

    def _show_caption(self, text: str, append: bool = True, prefix: Optional[str] = None) -> None:
        clean = " ".join(text.split())
        if prefix:
            clean = f"[{prefix}] {clean}"
        if append:
            if self.display_text:
                self.display_text = f"{self.display_text} {clean}"
            else:
                self.display_text = clean
            self.display_text = self.display_text[-5000:]
            body = self.display_text
        else:
            self.display_text = clean
            body = clean
        self.caption.configure(state="normal")
        self.caption.delete("1.0", "end")
        self.caption.insert("end", body)
        self.caption.see("end")
        self.caption.configure(state="disabled")

    def _show_live_partial(self, text: str) -> None:
        clean = " ".join(normalize_caption_text(text).split())
        if not clean:
            return
        body = f"{self.display_text} {clean}".strip()
        self.caption.configure(state="normal")
        self.caption.delete("1.0", "end")
        self.caption.insert("end", body)
        self.caption.see("end")
        self.caption.configure(state="disabled")

    def _translate_async(self, korean_text: str) -> None:
        target = LANGUAGES.get(self.language_var.get())
        if not target or not self.translator:
            return

        def worker() -> None:
            try:
                translated = self.translator.translate(korean_text, target)
                if translated:
                    self.events.put(CaptionEvent("translation", translated))
            except Exception as exc:
                self.events.put(CaptionEvent("error", f"Translation failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def start(self) -> None:
        if not self.api_key:
            if not self.ask_api_key():
                messagebox.showerror("Missing API key", "API 키가 필요합니다.")
                return
        self.captioner = RealtimeCaptioner(
            self.api_key,
            self.events,
            "gpt-realtime-whisper",
        )
        self.captioner.start()
        self.clear_caption()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Connecting...")

    def clear_caption(self) -> None:
        self.display_text = ""
        self.caption.configure(state="normal")
        self.caption.delete("1.0", "end")
        self.caption.configure(state="disabled")

    def ask_api_key(self) -> bool:
        key = simpledialog.askstring(
            "OpenAI API key",
            "OpenAI API 키를 입력하세요. 앱은 키를 파일에 저장하지 않습니다.",
            show="*",
            parent=self.root,
        )
        if not key:
            return False
        self.api_key = key.strip()
        self.translator = Translator(self.api_key)
        self.status_var.set("API key set for this session.")
        return True

    def stop(self) -> None:
        if self.captioner:
            self.captioner.stop()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def save_log(self) -> None:
        if not self.log_lines:
            self.status_var.set("No captions to save yet.")
            return
        output_dir = Path(__file__).resolve().parents[1] / "logs"
        output_dir.mkdir(exist_ok=True)
        path = output_dir / f"caption-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text("\n".join(self.log_lines), encoding="utf-8")
        self.status_var.set(f"Saved: {path}")

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.mainloop()

    def _close(self) -> None:
        self.stop()
        self.root.destroy()


if __name__ == "__main__":
    CaptionWindow().run()
