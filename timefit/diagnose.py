"""起動時・問い合わせ前の自己診断"""

from dataclasses import dataclass

from .config import AppConfig, default_config_path
from .engine import VoicevoxClient
from .errors import TimeFitError
from .llm import GeminiRewriter


@dataclass
class CheckItem:
    ok: bool
    message: str


def diagnose(cfg: AppConfig, engine: VoicevoxClient | None = None, check_llm: bool = True) -> list[CheckItem]:
    items = [CheckItem(True, f"設定ファイル: {default_config_path()}")]
    engine = engine or VoicevoxClient(cfg.engine_url, timeout=10)
    try:
        items.append(CheckItem(True, f"音声エンジンに接続できました（{cfg.engine_url}、バージョン {engine.version()}）"))
    except TimeFitError as e:
        items.append(CheckItem(False, str(e)))

    if not cfg.allow_rewrite:
        items.append(CheckItem(True, "要約はオフです（話速の調整だけで合わせます）"))
    elif not cfg.gemini_api_key:
        items.append(CheckItem(False, "Gemini の APIキーが未設定です（要約が必要な行は収まらない可能性があります）"))
    elif check_llm:
        try:
            GeminiRewriter(cfg.gemini_api_key, cfg.gemini_model, max_retries=0).rewrite("これは接続テストです。", 5)
            items.append(CheckItem(True, f"Gemini に接続できました（モデル {cfg.gemini_model}）"))
        except TimeFitError as e:
            items.append(CheckItem(False, str(e)))
    return items
