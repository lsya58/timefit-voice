"""台本の要約（リライト）"""

import re
import time
from typing import Protocol

from .errors import ApiKeyInvalidError, ApiKeyMissingError, LLMError


class Rewriter(Protocol):
    def rewrite(self, text: str, target_chars: int, previous: str | None = None) -> str:
        """text を target_chars 文字前後に要約して返す。previous は前回の長すぎた案。"""
        ...


SYSTEM_PROMPT = (
    "あなたは動画ナレーション原稿の編集者です。与えられた原稿を、読み上げ音声の尺に収まるよう短くします。\n"
    "ルール:\n"
    "- 元の意味・主張・重要な固有名詞・数字は残す\n"
    "- 新しい情報を足さない\n"
    "- 自然な話し言葉の日本語にする\n"
    "- 出力は書き換えた原稿の本文だけ。説明・前置き・かぎ括弧・改行・記号の装飾は付けない"
)


def build_prompt(text: str, target_chars: int, previous: str | None) -> str:
    prompt = f"次の原稿を{target_chars}文字以内に短くしてください。\n\n【原稿】\n{text}"
    if previous:
        prompt += (
            f"\n\n【前回の案（{len(previous)}文字）】\n{previous}\n"
            f"前回の案はまだ長すぎました。必ず{target_chars}文字以内にしてください。"
        )
    return prompt


_LABEL_RE = re.compile(r"^(要約|リライト後?|書き換え後?|短縮版|出力|回答|原稿)\s*[:：]\s*")
_QUOTE_PAIRS = [("「", "」"), ("『", "』"), ('"', '"'), ("“", "”")]


def sanitize_output(raw: str | None) -> str:
    """LLM の出力から前置き・括弧・改行などを取り除き、読み上げる本文だけにする"""
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^```[^\n]*\n?|```$", "", text).strip()
    lines = [_LABEL_RE.sub("", ln.strip()) for ln in text.splitlines() if ln.strip()]
    joined = ""
    for ln in lines:
        if joined and not joined.endswith(("。", "！", "？", "!", "?", "、")):
            joined += "。"
        joined += ln
    text = joined.strip()
    for left, right in _QUOTE_PAIRS:
        if text.startswith(left) and text.endswith(right) and len(text) > 2:
            text = text[1:-1].strip()
    return text


# リトライする HTTP ステータス（レート制限・一時的なサーバーエラー）
_RETRYABLE = {408, 429, 500, 502, 503, 504}


class GeminiRewriter:
    def __init__(self, api_key: str, model: str, max_retries: int = 3, sleep=time.sleep, client=None):
        if not api_key:
            raise ApiKeyMissingError(
                "Gemini の APIキーが設定されていません。設定画面で APIキーを入力してください。"
            )
        self.model = model
        self.max_retries = max_retries
        self._sleep = sleep
        if client is None:
            from google import genai  # 要約を使うときだけ読み込む（起動を速くするため）

            client = genai.Client(api_key=api_key)
        self._client = client

    def rewrite(self, text: str, target_chars: int, previous: str | None = None) -> str:
        from google.genai import errors, types

        prompt = build_prompt(text, target_chars, previous)
        config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.3)

        for attempt in range(self.max_retries + 1):
            try:
                res = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
            except errors.APIError as e:
                if e.code in (401, 403) or (e.code == 400 and "API key" in str(e)):
                    raise ApiKeyInvalidError(
                        "Gemini の APIキーが正しくないか、使えない状態です。設定画面でキーを確認してください。"
                    ) from None
                if e.code == 404:
                    raise LLMError(
                        f"Gemini のモデル「{self.model}」が見つかりません。設定画面でモデル名を確認してください。"
                    ) from None
                if e.code in _RETRYABLE and attempt < self.max_retries:
                    self._sleep(2 ** (attempt + 1))
                    continue
                if e.code == 429:
                    raise LLMError(
                        "Gemini の利用回数の上限に達しました。しばらく待ってから再実行してください。"
                    ) from None
                raise LLMError(f"Gemini でエラーが発生しました（{e.code}）: {e.message}") from None
            except Exception as e:  # 通信エラーなど
                if attempt < self.max_retries:
                    self._sleep(2 ** (attempt + 1))
                    continue
                raise LLMError(f"Gemini に接続できませんでした。ネット接続を確認してください。（{e}）") from None

            result = sanitize_output(res.text)
            if not result:
                raise LLMError("Gemini から空の結果が返ってきました。")
            return result
        raise LLMError("Gemini の呼び出しに失敗しました。")  # ここには来ない
