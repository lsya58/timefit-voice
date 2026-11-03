"""デスクトップ画面（Tkinter）

処理は別スレッド1本で行い、画面の更新はキュー経由でメインスレッドから行う。
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__, edition
from .batch import run_job
from .config import AppConfig, load_config, save_config
from .diagnose import diagnose
from .engine import VoicevoxClient
from .errors import TimeFitError

PAD = {"padx": 6, "pady": 4}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__}（{edition.edition_label()}）")
        self.geometry("900x680")
        self.minsize(760, 560)

        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_output: Path | None = None

        try:
            self.cfg = load_config()
        except TimeFitError as e:
            messagebox.showwarning(APP_NAME, f"{e}\n既定の設定で起動します。")
            self.cfg = AppConfig()

        self._build()
        self.after(100, self._poll_events)
        self.after(300, self.refresh_speakers)

    # ---------- 画面 ----------
    def _build(self):
        files = ttk.LabelFrame(self, text="ファイル")
        files.pack(fill="x", **PAD)
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        ttk.Label(files, text="台本（CSV / SRT / VTT）").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(files, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", **PAD)
        ttk.Button(files, text="選ぶ...", command=self.choose_input).grid(row=0, column=2, **PAD)
        ttk.Label(files, text="出力フォルダ").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(files, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", **PAD)
        ttk.Button(files, text="選ぶ...", command=self.choose_output).grid(row=1, column=2, **PAD)
        files.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self, text="設定")
        opts.pack(fill="x", **PAD)
        ttk.Label(opts, text="話者").grid(row=0, column=0, sticky="w", **PAD)
        self.speaker_var = tk.StringVar(value=f"話者ID [{self.cfg.default_speaker}]")
        self.speaker_box = ttk.Combobox(opts, textvariable=self.speaker_var, width=40)
        self.speaker_box.grid(row=0, column=1, sticky="w", **PAD)
        ttk.Button(opts, text="一覧を更新", command=self.refresh_speakers).grid(row=0, column=2, sticky="w", **PAD)

        ttk.Label(opts, text="合わせ方").grid(row=1, column=0, sticky="w", **PAD)
        self.mode_var = tk.StringVar(value=self.cfg.fit_mode)
        mode = ttk.Frame(opts)
        mode.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(mode, text="ぴったり（足りない分は無音）", value="exact", variable=self.mode_var).pack(side="left")
        ttk.Radiobutton(mode, text="以内（短ければそのまま）", value="within", variable=self.mode_var).pack(side="left", padx=12)

        ttk.Label(opts, text="話速の上限").grid(row=2, column=0, sticky="w", **PAD)
        self.speed_var = tk.DoubleVar(value=self.cfg.max_speed)
        ttk.Spinbox(opts, from_=1.0, to=2.0, increment=0.05, textvariable=self.speed_var, width=6).grid(
            row=2, column=1, sticky="w", **PAD)
        self.rewrite_var = tk.BooleanVar(value=self.cfg.allow_rewrite)
        ttk.Checkbutton(opts, text="収まらないときは AI で要約する（Gemini）", variable=self.rewrite_var).grid(
            row=3, column=1, columnspan=2, sticky="w", **PAD)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", **PAD)
        self.run_btn = ttk.Button(buttons, text="▶ 実行", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="中止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(buttons, text="APIキー・詳細設定", command=self.open_settings).pack(side="left", padx=6)
        ttk.Button(buttons, text="動作チェック", command=self.run_diagnose).pack(side="left", padx=6)
        self.open_btn = ttk.Button(buttons, text="出力フォルダを開く", command=self.open_output, state="disabled")
        self.open_btn.pack(side="right")

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **PAD)

        cols = ("no", "status", "target", "actual", "speed", "text")
        self.table = ttk.Treeview(self, columns=cols, show="headings", height=8)
        for c, label, w in [("no", "番号", 50), ("status", "状態", 80), ("target", "目標秒", 70),
                            ("actual", "生成秒", 70), ("speed", "話速", 60), ("text", "最終原稿", 520)]:
            self.table.heading(c, text=label)
            self.table.column(c, width=w, anchor="w" if c == "text" else "center", stretch=(c == "text"))
        self.table.tag_configure("failed", background="#ffe0e0")
        self.table.tag_configure("rewrite", background="#fff6d6")
        self.table.pack(fill="both", expand=True, **PAD)

        self.log_box = tk.Text(self, height=9, wrap="char", state="disabled")
        self.log_box.pack(fill="both", **PAD)

        footer = "クレジット表記が必要です（出力される credits.txt を参照）。"
        if edition.is_free():
            footer = f"無料版: 1回 {edition.FREE_LINE_LIMIT} 行まで。製品版は行数無制限です。  " + footer
        ttk.Label(self, text=footer, foreground="#555").pack(anchor="w", **PAD)

    # ---------- 操作 ----------
    def choose_input(self):
        path = filedialog.askopenfilename(
            title="台本ファイルを選ぶ",
            filetypes=[("台本", "*.csv *.srt *.vtt *.tsv *.txt"), ("すべて", "*.*")],
        )
        if path:
            self.input_var.set(path)
            if not self.output_var.get():
                p = Path(path)
                self.output_var.set(str(p.with_name(p.stem + "_output")))

    def choose_output(self):
        path = filedialog.askdirectory(title="出力フォルダを選ぶ")
        if path:
            self.output_var.set(path)

    def refresh_speakers(self):
        try:
            styles = VoicevoxClient(self.cfg.engine_url, timeout=5).speakers()
        except TimeFitError as e:
            self.log(f"話者一覧を取得できませんでした: {e}")
            return
        labels = [s.label for s in styles]
        self.speaker_box["values"] = labels
        current = next((lb for lb in labels if lb.endswith(f"[{self.cfg.default_speaker}]")), None)
        if current:
            self.speaker_var.set(current)

    def _selected_speaker(self) -> int:
        text = self.speaker_var.get().strip()
        try:
            return int(text.rsplit("[", 1)[-1].rstrip("]")) if "[" in text else int(text)
        except ValueError:
            raise TimeFitError("話者を一覧から選ぶか、話者IDを数字で入力してください。") from None

    def _collect_settings(self):
        self.cfg.default_speaker = self._selected_speaker()
        self.cfg.fit_mode = self.mode_var.get()
        try:
            self.cfg.max_speed = float(self.speed_var.get())
        except (tk.TclError, ValueError):
            raise TimeFitError("話速の上限は数字で入力してください。") from None
        self.cfg.allow_rewrite = bool(self.rewrite_var.get())
        save_config(self.cfg)  # 次回起動時も同じ設定にする

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        input_path = self.input_var.get().strip()
        if not input_path:
            messagebox.showinfo(APP_NAME, "台本ファイルを選んでください。")
            return
        output = self.output_var.get().strip() or str(Path(input_path).with_name(Path(input_path).stem + "_output"))
        try:
            self._collect_settings()
        except TimeFitError as e:
            messagebox.showerror(APP_NAME, str(e))
            return

        self.table.delete(*self.table.get_children())
        self.progress["value"] = 0
        self.cancel_event.clear()
        self._set_running(True)
        self.worker = threading.Thread(target=self._work, args=(input_path, output), daemon=True)
        self.worker.start()

    def stop(self):
        self.cancel_event.set()
        self.log("中止を受け付けました。今の行が終わったら止まります。")

    def _work(self, input_path, output):
        try:
            summary = run_job(
                input_path, output, self.cfg,
                log=lambda m: self.events.put(("log", m)),
                on_progress=lambda i, n, r: self.events.put(("progress", (i, n, r))),
                cancel=self.cancel_event,
            )
            self.events.put(("done", summary))
        except TimeFitError as e:
            self.events.put(("error", str(e)))
        except Exception as e:  # 想定外のエラーも画面に出して、ログを送ってもらえるようにする
            self.events.put(("error", f"想定外のエラーが発生しました: {type(e).__name__}: {e}"))

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    i, n, r = payload
                    self.progress["maximum"] = n
                    self.progress["value"] = i
                    tag = "failed" if not r.ok else ("rewrite" if r.rewrite_attempts else "")
                    self.table.insert("", "end", tags=(tag,), values=(
                        i, r.status.value, f"{r.target_sec:.2f}", f"{r.duration:.2f}", f"{r.speed:.2f}", r.text))
                elif kind == "done":
                    self._set_running(False)
                    self.last_output = payload.output_dir
                    self.open_btn["state"] = "normal"
                    msg = f"完了しました（{len(payload.results)}行）。"
                    if payload.failed:
                        msg += f"\n{payload.failed}行は目標に収まりませんでした（赤い行）。原稿を短くしてください。"
                    if payload.skipped_by_limit:
                        msg += f"\n無料版のため {payload.skipped_by_limit}行は処理していません。"
                    messagebox.showinfo(APP_NAME, msg)
                elif kind == "error":
                    self._set_running(False)
                    self.log(f"【エラー】{payload}")
                    messagebox.showerror(APP_NAME, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _set_running(self, running: bool):
        self.run_btn["state"] = "disabled" if running else "normal"
        self.stop_btn["state"] = "normal" if running else "disabled"

    def log(self, message: str):
        self.log_box["state"] = "normal"
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box["state"] = "disabled"

    def open_output(self):
        if not self.last_output:
            return
        if sys.platform == "win32":
            os.startfile(self.last_output)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(self.last_output)])

    def run_diagnose(self):
        self.log("動作チェック中...")
        self.update_idletasks()
        items = diagnose(self.cfg)
        text = "\n".join(("✅ " if i.ok else "❌ ") + i.message for i in items)
        self.log(text)
        (messagebox.showinfo if all(i.ok for i in items) else messagebox.showwarning)(APP_NAME, text)

    def open_settings(self):
        SettingsDialog(self, self.cfg)


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: App, cfg: AppConfig):
        super().__init__(master)
        self.title("APIキー・詳細設定")
        self.cfg = cfg
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.vars = {
            "gemini_api_key": tk.StringVar(value=cfg.gemini_api_key),
            "gemini_model": tk.StringVar(value=cfg.gemini_model),
            "engine_url": tk.StringVar(value=cfg.engine_url),
            "credit_prefix": tk.StringVar(value=cfg.credit_prefix),
            "min_pause_scale": tk.StringVar(value=str(cfg.min_pause_scale)),
            "max_rewrite_attempts": tk.StringVar(value=str(cfg.max_rewrite_attempts)),
        }
        rows = [
            ("Gemini APIキー", "gemini_api_key", "*"),
            ("Gemini モデル名", "gemini_model", None),
            ("音声エンジンのURL", "engine_url", None),
            ("クレジット表記の頭", "credit_prefix", None),
            ("間を詰める下限（0〜1）", "min_pause_scale", None),
            ("要約の最大回数", "max_rewrite_attempts", None),
        ]
        for r, (label, key, show) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", **PAD)
            ttk.Entry(self, textvariable=self.vars[key], width=46, show=show or "").grid(row=r, column=1, **PAD)
        hint = ("APIキーは Google AI Studio で無料で発行できます（手順はマニュアル参照）。\n"
                "AivisSpeech など VOICEVOX 互換のエンジンは URL を変えると使えます（例: http://127.0.0.1:10101）。")
        ttk.Label(self, text=hint, foreground="#555").grid(row=len(rows), column=0, columnspan=2, sticky="w", **PAD)
        btns = ttk.Frame(self)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="e", **PAD)
        ttk.Button(btns, text="保存", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="キャンセル", command=self.destroy).pack(side="left")

    def save(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        try:
            self.cfg.gemini_api_key = v["gemini_api_key"]
            self.cfg.gemini_model = v["gemini_model"] or AppConfig.gemini_model
            self.cfg.engine_url = v["engine_url"] or AppConfig.engine_url
            self.cfg.credit_prefix = v["credit_prefix"] or AppConfig.credit_prefix
            self.cfg.min_pause_scale = float(v["min_pause_scale"])
            self.cfg.max_rewrite_attempts = int(v["max_rewrite_attempts"])
            path = save_config(self.cfg)
        except ValueError:
            messagebox.showerror(APP_NAME, "数字の欄に数字以外が入っています。", parent=self)
            return
        except TimeFitError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        messagebox.showinfo(APP_NAME, f"保存しました。\n{path}", parent=self)
        self.destroy()


def main():
    App().mainloop()
