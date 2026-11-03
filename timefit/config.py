"""設定の読み書き。保存先は Windows なら %APPDATA%\\TimeFitVoice\\config.json"""

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .errors import ConfigError
from .fitter import FitMode, FitSettings


@dataclass
class AppConfig:
    gemini_api_key: str = ""
    # 既定は軽量モデル（3案を約1秒で返す。無料枠の回数も多い）。上限・混雑時は予備モデルへ自動で切り替える
    gemini_model: str = "gemini-flash-lite-latest"
    # 混雑・利用上限のときに切り替える予備モデル（カンマ区切りで複数可）
    gemini_fallback_model: str = "gemini-3.8-flash"
    # 思考の深さ（low / medium / high、空欄ならモデルの既定）。短い文の要約は low で十分で速い
    gemini_thinking: str = "low"
    engine_url: str = "http://127.0.0.1:50021"
    credit_prefix: str = "VOICEVOX"
    default_speaker: int = 3
    max_speed: float = 1.3
    min_pause_scale: float = 0.6
    fit_mode: str = FitMode.EXACT.value
    trim_tolerance_ms: int = 80
    max_rewrite_attempts: int = 3
    allow_rewrite: bool = True

    def fit_settings(self) -> FitSettings:
        return FitSettings(
            max_speed=self.max_speed,
            min_pause_scale=self.min_pause_scale,
            mode=FitMode(self.fit_mode),
            trim_tolerance_ms=self.trim_tolerance_ms,
            max_rewrite_attempts=self.max_rewrite_attempts,
            allow_rewrite=self.allow_rewrite,
        )

    def validate(self) -> None:
        if not 1.0 <= self.max_speed <= 2.0:
            raise ConfigError("最大話速は 1.0〜2.0 の範囲で指定してください。")
        if not 0.0 <= self.min_pause_scale <= 1.0:
            raise ConfigError("間の最小倍率は 0.0〜1.0 の範囲で指定してください。")
        if self.fit_mode not in {m.value for m in FitMode}:
            raise ConfigError("合わせ方は exact か within を指定してください。")
        if self.gemini_thinking.lower() not in ("", "minimal", "low", "medium", "high"):
            raise ConfigError("思考の深さは low / medium / high のいずれか（または空欄）にしてください。")
        if self.max_rewrite_attempts < 0:
            raise ConfigError("要約の最大回数は 0 以上にしてください。")


def default_config_path() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "TimeFitVoice" / "config.json"
    return Path.home() / ".config" / "timefit-voice" / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    cfg = AppConfig()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"設定ファイルを読み込めませんでした: {path}（{e}）") from None
        _migrate(data)
        known = {f.name for f in fields(AppConfig)}
        for key, value in data.items():
            if key not in known:
                continue
            kind = type(getattr(cfg, key))  # 既定値の型に揃える（手で編集された値のため）
            try:
                setattr(cfg, key, kind(value))
            except (TypeError, ValueError):
                raise ConfigError(f"設定ファイルの「{key}」の値が正しくありません: {value!r}") from None

    # 開発用: .env / 環境変数の GEMINI_API_KEY を、設定ファイルにキーがないときだけ使う
    if not cfg.gemini_api_key:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        cfg.gemini_api_key = os.environ.get("GEMINI_API_KEY", "")

    cfg.validate()
    return cfg


def _migrate(data: dict) -> None:
    """古い版の既定値のまま保存されている設定を、今の既定値に合わせる（ユーザーが変えた値は触らない）"""
    if "gemini_thinking" not in data:  # v0.4.0 以前
        if data.get("gemini_model") == "gemini-3.8-flash" and data.get("gemini_fallback_model") in (None, "gemini-flash-lite-latest"):
            data["gemini_model"] = AppConfig.gemini_model
            data["gemini_fallback_model"] = AppConfig.gemini_fallback_model


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    cfg.validate()
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
