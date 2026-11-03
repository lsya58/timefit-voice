"""デスクトップ画面（pywebview + HTML/CSS）

画面は timefit/ui/web/ の HTML・CSS・JS で描き、Windows 標準の Edge WebView2 で表示する。
JS からは window.pywebview.api.<メソッド名>() で下の Api クラスを呼び、
Python からは window.tf.onEvent({...}) で画面に通知する。

pywebview は「_」で始まらない属性を JS に公開するので、内部の状態は必ず「_」付きにする。
"""

import base64
import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from .. import APP_NAME, __version__, edition
from ..batch import JobSummary, estimate_text, refit_line, run_job, suggest_candidates
from ..config import AppConfig, load_config, save_config
from ..diagnose import diagnose
from ..engine import VoicevoxClient
from ..errors import TimeFitError
from ..fitter import FitResult
from ..inputs import ScriptLine, load_script

WEB_DIR = Path(__file__).parent / "web"
SCRIPT_TYPES = ("台本ファイル (*.csv;*.srt;*.vtt;*.tsv;*.txt)", "すべてのファイル (*.*)")

# 状態ごとの CSS 用キー
STATUS_KEYS = {"等倍": "natural", "間を調整": "pause", "話速調整": "speed", "要約": "rewrite", "収まらず": "failed"}


def _line_dict(line: ScriptLine, default_speaker: int) -> dict:
    return {
        "index": line.index,
        "text": line.text,
        "target": line.target_sec,
        "start": line.start_sec,
        "speaker": line.speaker_id if line.speaker_id is not None else default_speaker,
        "speakerFromFile": line.speaker_id is not None,
    }


def sample_path() -> Path:
    """同梱のサンプル字幕。exe では展開先（_MEIPASS）、開発中はリポジトリ直下の samples/"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "samples" / "sample.srt"


def _result_dict(index: int, r: FitResult, filename: str, approved: bool = False) -> dict:
    return {
        "index": index,
        "status": r.status.value,
        "statusKey": STATUS_KEYS[r.status.value],
        "ok": r.ok,
        "target": r.target_sec,
        "duration": r.duration,
        "speech": r.speech_sec,
        "speed": r.speed,
        "pause": r.pause_scale,
        "text": r.text,
        "original": r.original_text,
        "rewritten": r.rewritten,
        "candidates": list(r.candidates),
        "approved": approved,
        "message": r.message,
        "file": filename,
    }


class Api:
    def __init__(self):
        self._window = None
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._results: dict[int, FitResult] = {}
        self._summary: JobSummary | None = None
        self._output_dir: Path | None = None
        self._config_error = None
        try:
            self._cfg = load_config()
        except TimeFitError as e:
            self._cfg = AppConfig()
            self._config_error = str(e)

    # ---------- Python → 画面 ----------
    def _emit(self, kind: str, **payload):
        if self._window is None:
            return
        data = json.dumps({"type": kind, **payload}, ensure_ascii=False)
        self._window.evaluate_js(f"window.tf && window.tf.onEvent({data})")

    def _attach(self, window):
        self._window = window

    # ---------- 起動時 ----------
    def init(self) -> dict:
        return {
            "app": APP_NAME,
            "version": __version__,
            "edition": edition.EDITION,
            "editionLabel": edition.edition_label(),
            "freeLimit": edition.FREE_LINE_LIMIT if edition.is_free() else None,
            "settings": self.get_settings(),
            "configError": self._config_error,
        }

    def get_settings(self) -> dict:
        d = asdict(self._cfg)
        key = d.pop("gemini_api_key")
        d["hasApiKey"] = bool(key)
        d["apiKeyHint"] = f"…{key[-4:]}" if len(key) >= 8 else ""
        return d

    def save_settings(self, data: dict) -> dict:
        """画面の設定を保存する。gemini_api_key は空なら変更しない。"""
        cfg = self._cfg
        before = asdict(cfg)
        try:
            for key, value in (data or {}).items():
                if key == "gemini_api_key":
                    if value and value.strip():
                        cfg.gemini_api_key = value.strip()
                    continue
                if key == "clear_api_key" and value:
                    cfg.gemini_api_key = ""
                    continue
                if hasattr(cfg, key) and not key.startswith("_"):
                    kind = type(getattr(cfg, key))
                    setattr(cfg, key, kind(value))
            save_config(cfg)
        except (TimeFitError, TypeError, ValueError) as e:
            for k, v in before.items():  # 失敗したら元に戻す
                setattr(cfg, k, v)
            msg = str(e) if isinstance(e, TimeFitError) else "設定の値が正しくありません。"
            return {"ok": False, "error": msg}
        return {"ok": True, "settings": self.get_settings()}

    def speakers(self) -> dict:
        try:
            styles = VoicevoxClient(self._cfg.engine_url, timeout=5).speakers()
        except TimeFitError as e:
            return {"ok": False, "error": str(e), "items": []}
        return {
            "ok": True,
            "items": [{"id": s.style_id, "character": s.character, "style": s.style} for s in styles],
        }

    # ---------- ファイル ----------
    def choose_input(self) -> dict | None:
        import webview

        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=SCRIPT_TYPES)
        if not paths:
            return None
        return self.preview(paths[0])

    def choose_output(self) -> str | None:
        import webview

        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return paths[0] if paths else None

    def preview(self, path: str) -> dict:
        try:
            script = load_script(path)
        except TimeFitError as e:
            return {"ok": False, "error": str(e)}
        p = Path(path)
        return {
            "ok": True,
            "path": str(p),
            "name": p.name,
            "kind": script.kind,
            "output": str(p.with_name(p.stem + "_output")),
            "lines": [_line_dict(ln, self._cfg.default_speaker) for ln in script.lines],
        }

    def sample(self) -> dict:
        path = sample_path()
        if not path.exists():
            return {"ok": False, "error": f"サンプルが見つかりません: {path}"}
        return self.preview(str(path))

    def _on_drop(self, event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        path = next((f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")), None)
        if path:
            self._emit("file", preview=self.preview(path))

    # ---------- 実行 ----------
    def start(self, params: dict) -> dict:
        if self._worker and self._worker.is_alive():
            return {"ok": False, "error": "処理中です。"}
        params = params or {}
        input_path = params.get("input")
        if not input_path:
            return {"ok": False, "error": "台本ファイルを選んでください。"}
        output = params.get("output") or str(Path(input_path).with_name(Path(input_path).stem + "_output"))
        saved = self.save_settings({k: params[k] for k in
                                    ("default_speaker", "fit_mode", "max_speed", "allow_rewrite") if k in params})
        if not saved["ok"]:
            return saved

        self._cancel.clear()
        self._results.clear()
        self._summary = None
        self._output_dir = Path(output)
        self._worker = threading.Thread(target=self._work, args=(input_path, output), daemon=True)
        self._worker.start()
        return {"ok": True, "output": output}

    def _work(self, input_path, output):
        try:
            summary = run_job(
                input_path, output, self._cfg,
                log=lambda m: self._emit("log", message=m),
                on_line_start=lambda i, n, line: self._emit("lineStart", index=line.index, position=i, total=n),
                on_progress=self._on_progress,
                cancel=self._cancel,
            )
            self._summary = summary
            self._emit("done", summary={
                "count": len(summary.results),
                "failed": summary.failed,
                "skipped": summary.skipped_by_limit,
                "cancelled": summary.cancelled,
                "counts": summary.counts(),
                "output": str(summary.output_dir),
            })
        except TimeFitError as e:
            self._emit("error", message=str(e))
        except Exception as e:  # 想定外のエラーも画面に出して、問い合わせ時に送ってもらえるようにする
            self._emit("error", message=f"想定外のエラーが発生しました: {type(e).__name__}: {e}")

    def _on_progress(self, i, n, line, result, filename):
        self._results[line.index] = result
        self._emit("lineDone", position=i, total=n, result=_result_dict(line.index, result, filename))

    def cancel(self) -> None:
        self._cancel.set()

    # ---------- 要約の確認・作り直し ----------
    def _busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _summary_or_error(self):
        if self._busy():
            return None, {"ok": False, "error": "処理中です。終わってから操作してください。"}
        if self._summary is None:
            return None, {"ok": False, "error": "先に「尺合わせを開始」で処理してください。"}
        return self._summary, None

    def suggest(self, index: int) -> dict:
        """AI に別の案を出してもらう"""
        summary, err = self._summary_or_error()
        if err:
            return err
        try:
            cands = suggest_candidates(summary, int(index), self._cfg, log=lambda m: self._emit("log", message=m))
        except TimeFitError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "candidates": cands}

    def estimate(self, index: int, text: str) -> dict:
        """入力中の文が、目標に収まりそうかを見積もる（合成はしない）"""
        summary, err = self._summary_or_error()
        if err:
            return err
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "原稿が空です。"}
        try:
            pos = summary.position(int(index))
            est = estimate_text(text, summary.speakers[pos], summary.lines[pos].target_sec, self._cfg)
        except TimeFitError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **est}

    def apply(self, index: int, text: str) -> dict:
        """選んだ／書いた文で1行だけ作り直す"""
        summary, err = self._summary_or_error()
        if err:
            return err
        index = int(index)
        try:
            result = refit_line(summary, index, text, self._cfg, log=lambda m: self._emit("log", message=m))
        except TimeFitError as e:
            return {"ok": False, "error": str(e)}
        self._results[index] = result
        pos = summary.position(index)
        return {"ok": True, "result": _result_dict(index, result, summary.files[pos], True)}

    def approve(self, index: int) -> dict:
        summary, err = self._summary_or_error()
        if err:
            return err
        summary.approved.add(int(index))
        return {"ok": True}

    def audio(self, index: int) -> str | None:
        """1行分の音声を data URL で返す（画面で試聴するため）"""
        r = self._results.get(int(index))
        if r is None:
            return None
        return "data:audio/wav;base64," + base64.b64encode(r.wav.to_bytes()).decode("ascii")

    def combined_audio(self) -> str | None:
        path = self._output_dir / "combined.wav" if self._output_dir else None
        if not path or not path.exists():
            return None
        return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    # ---------- その他 ----------
    def diagnose(self) -> list[dict]:
        return [{"ok": i.ok, "message": i.message} for i in diagnose(self._cfg)]

    def open_output(self) -> None:
        if self._output_dir and self._output_dir.exists():
            _open_path(str(self._output_dir))

    def open_url(self, url: str) -> None:
        if url.startswith("https://"):
            import webbrowser

            webbrowser.open(url)


def _open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


def build_html() -> str:
    """HTML に CSS と JS を埋め込んで1枚にする（exe 化したときのパスの扱いを簡単にするため）"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    return (html.replace("/*__STYLE__*/", css).replace("/*__SCRIPT__*/", js))


def main() -> None:
    import webview
    from webview.dom import DOMEventHandler

    api = Api()
    window = webview.create_window(
        f"{APP_NAME} {__version__}",
        html=build_html(),
        js_api=api,
        width=1280,
        height=820,
        min_size=(980, 640),
        background_color="#0E0F13",
    )
    api._attach(window)

    def on_loaded():
        # ファイルのドラッグ＆ドロップ（画面のどこに落としても受け付ける）
        try:
            doc = window.dom.document
            doc.events.dragenter += DOMEventHandler(lambda e: None, True, True)
            doc.events.dragover += DOMEventHandler(lambda e: None, True, True, debounce=500)
            doc.events.drop += DOMEventHandler(api._on_drop, True, True)
        except Exception:
            pass  # 使えない環境では「選ぶ」ボタンだけで動く

    webview.start(on_loaded, private_mode=False)
