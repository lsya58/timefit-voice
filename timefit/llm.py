"""台本の要約（リライト）"""

import json
import re
import threading
import time
from typing import Protocol

from .errors import ApiKeyInvalidError, ApiKeyMissingError, LLMError


class Rewriter(Protocol):
    def candidates(
        self, text: str, target_chars: int, previous: str | None = None, avoid: list[str] | None = None, n: int = 3,
    ) -> list[str]:
        """text を target_chars 文字以内に要約した案を n 個返す（重複なし・1個以上）。

        previous は前回の長すぎた案、avoid は「これとは違う言い回しにしてほしい」案。
        """
        ...


SYSTEM_PROMPT = (
    "あなたは動画ナレーション原稿の編集者です。与えられた原稿を、読み上げ音声の尺に収まるよう短くします。\n"
    "ルール:\n"
    "- 元の意味・主張・重要な固有名詞・数字は残す\n"
    "- 新しい情報を足さない\n"
    "- 数字は算用数字のまま書く（例: 260年）\n"
    "- 自然な話し言葉の日本語にする\n"
    "- 各案は原稿の本文だけ。説明・前置き・かぎ括弧・改行・記号の装飾は付けない"
)

# 案ごとの方針（n 個目までを使う）
CANDIDATE_STYLES = [
    "重要な情報（固有名詞・数字・事実）をできるだけ多く残す",
    "話し言葉として自然に聞こえることを優先する",
    "要点だけに絞って、さらに短くする（目安の8割ほど）",
    "語順や言い回しを大きく変えて、テンポよく聞こえるようにする",
]


def build_prompt(
    text: str, target_chars: int, previous: str | None, avoid: list[str] | None = None, n: int = 3,
) -> str:
    styles = "\n".join(f"- 案{i + 1}: {CANDIDATE_STYLES[i % len(CANDIDATE_STYLES)]}" for i in range(n))
    prompt = (
        f"次の原稿を{target_chars}文字以内に短くした案を{n}個作ってください。\n"
        f"{styles}\n"
        f"出力は JSON の文字列配列だけにしてください（例: [\"案1\", \"案2\"]）。\n\n"
        f"【原稿】\n{text}"
    )
    if previous:
        prompt += (
            f"\n\n【前回の案（{len(previous)}文字）】\n{previous}\n"
            f"前回の案はまだ長すぎました。必ず{target_chars}文字以内にしてください。"
        )
    if avoid:
        joined = "\n".join(f"- {a}" for a in avoid)
        prompt += f"\n\n【すでに出した案】\n{joined}\nこれらとは違う言い回しにしてください。"
    return prompt


def parse_candidates(raw: str | None) -> list[str]:
    """LLM の出力（JSON 配列のはず）を案のリストにする。JSON でなければ全体を1案として扱う。"""
    if not raw:
        return []
    text = re.sub(r"^```[^\n]*\n?|```$", "", raw.strip()).strip()
    items: list[str]
    try:
        data = json.loads(text)
        if isinstance(data, dict):  # {"candidates": [...]} のように包まれていた場合
            data = next((v for v in data.values() if isinstance(v, list)), list(data.values()))
        if isinstance(data, str):
            items = [data]
        elif isinstance(data, list):
            items = [str(d.get("text", "")) if isinstance(d, dict) else str(d) for d in data]
        else:
            items = [text]
    except (json.JSONDecodeError, AttributeError):
        items = [text]
    out: list[str] = []
    for item in items:
        cleaned = sanitize_output(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


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


# ---------- Gemini ----------

# モデルごとの「いつまで使わないか」（アプリ起動中は共有する）。
# 1日の上限に達したモデルに何度も問い合わせると、待ち時間と回数の枠を無駄にするため。
_blocked_until: dict[str, float] = {}
_unsupported_thinking: set[str] = set()
_state_lock = threading.Lock()

BUSY_COOLDOWN_SEC = 60        # 混雑（503）・タイムアウトのとき、そのモデルを休ませる時間
SHORT_WAIT_LIMIT_SEC = 10     # この秒数までの「待ってね」はその場で待つ。長ければ予備に切り替える
REQUEST_TIMEOUT_MS = 30_000


def reset_model_state() -> None:
    """テスト用: モデルの休止状態を消す"""
    with _state_lock:
        _blocked_until.clear()
        _unsupported_thinking.clear()


def _block(model: str, seconds: float) -> None:
    with _state_lock:
        _blocked_until[model] = max(_blocked_until.get(model, 0.0), time.monotonic() + seconds)


def _blocked_for(model: str) -> float:
    with _state_lock:
        return max(0.0, _blocked_until.get(model, 0.0) - time.monotonic())


def _retry_delay(e) -> float | None:
    """429 の応答に入っている「何秒後に再試行してよいか」"""
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', json.dumps(getattr(e, "details", None) or {}))
    return float(m.group(1)) if m else None


def _is_daily_quota(e) -> bool:
    return "PerDay" in json.dumps(getattr(e, "details", None) or {})


def _fmt_wait(sec: float) -> str:
    if sec >= 3600:
        return f"約{sec / 3600:.1f}時間"
    if sec >= 60:
        return f"約{sec / 60:.0f}分"
    return f"{sec:.0f}秒"


class _ModelUnavailable(Exception):
    """このモデルは今使えない（混雑・上限・存在しない）。予備モデルに切り替える合図。"""


class GeminiRewriter:
    """Gemini で要約案を作る。

    on_event には計測用の辞書が渡される（原稿の本文や APIキーは含めない）:
      {"type": "gemini", "model", "result": "ok|503|429-day|429-minute|timeout|network|400|...", "sec",
       "in_tokens", "out_tokens", "thought_tokens"}
      {"type": "wait", "sec", "reason"}
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_models: tuple[str, ...] | list[str] = (),
        max_retries: int = 1,
        sleep=time.sleep,
        client=None,
        log=lambda _m: None,
        thinking_level: str = "low",
        on_event=lambda _e: None,
        timeout_ms: int = REQUEST_TIMEOUT_MS,
    ):
        if not api_key:
            raise ApiKeyMissingError(
                "Gemini の APIキーが設定されていません。設定画面で APIキーを入力してください。"
            )
        # 重複や空欄を除いて、使う順に並べる
        self.models = list(dict.fromkeys(m for m in [model, *fallback_models] if m))
        self.max_retries = max_retries
        self.thinking_level = (thinking_level or "").upper()
        self._sleep = sleep
        self._log = log
        self._on_event = on_event
        if client is None:
            from google import genai  # 要約を使うときだけ読み込む（起動を速くするため）
            from google.genai import types

            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        self._client = client

    @property
    def model(self) -> str:
        return self.models[0]

    def candidates(
        self, text: str, target_chars: int, previous: str | None = None, avoid: list[str] | None = None, n: int = 3,
    ) -> list[str]:
        prompt = build_prompt(text, target_chars, previous, avoid, n)
        reasons = []
        for model in self.models:
            wait = _blocked_for(model)
            if wait > 0:
                reasons.append(f"{model}: あと{_fmt_wait(wait)}休止中")
                continue
            try:
                return self._call(model, prompt)
            except _ModelUnavailable as e:
                reasons.append(f"{model}: {e}")
                nxt = next((m for m in self.models[self.models.index(model) + 1:] if _blocked_for(m) == 0), None)
                if nxt:
                    self._log(f"     {model} が使えないため、{nxt} に切り替えます（{e}）")
        raise LLMError(
            "Gemini が混雑しているか利用上限に達していて、要約できませんでした。"
            "少し時間をおいて再実行するか、設定で別のモデルを指定してください。（" + " / ".join(reasons) + "）"
        )

    def _config(self, model: str):
        from google.genai import types

        kw = dict(system_instruction=SYSTEM_PROMPT, temperature=0.5, response_mime_type="application/json")
        if self.thinking_level and model not in _unsupported_thinking:
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)
        return types.GenerateContentConfig(**kw)

    def _event(self, **kw):
        try:
            self._on_event(kw)
        except Exception:
            pass

    def _wait(self, sec: float, reason: str):
        self._event(type="wait", sec=sec, reason=reason)
        self._sleep(sec)

    def _call(self, model: str, prompt: str) -> list[str]:
        from google.genai import errors

        attempt = 0
        thinking_retry_done = False
        while True:
            t0 = time.perf_counter()
            try:
                res = self._client.models.generate_content(model=model, contents=prompt, config=self._config(model))
            except errors.APIError as e:
                sec = time.perf_counter() - t0
                code = e.code
                if code in (401, 403) or (code == 400 and "API key" in str(e)):
                    self._event(type="gemini", model=model, result="auth", sec=sec)
                    raise ApiKeyInvalidError(
                        "Gemini の APIキーが正しくないか、使えない状態です。設定画面でキーを確認してください。"
                    ) from None
                if code == 400 and "hinking" in str(e.message) and not thinking_retry_done:
                    # このモデルは指定した思考レベルに対応していない → 指定なしでやり直す
                    self._event(type="gemini", model=model, result="400-thinking", sec=sec)
                    with _state_lock:
                        _unsupported_thinking.add(model)
                    thinking_retry_done = True
                    continue
                if code == 404:
                    self._event(type="gemini", model=model, result="404", sec=sec)
                    _block(model, 24 * 3600)
                    raise _ModelUnavailable("モデルが見つかりません") from None
                if code == 429:
                    delay = _retry_delay(e)
                    if _is_daily_quota(e) or (delay or 0) > SHORT_WAIT_LIMIT_SEC:
                        self._event(type="gemini", model=model, result="429-day" if _is_daily_quota(e) else "429-minute", sec=sec)
                        wait = delay or 3600
                        _block(model, wait)
                        label = "1日の利用上限" if _is_daily_quota(e) else "利用上限"
                        raise _ModelUnavailable(f"{label}。回復まで{_fmt_wait(wait)}") from None
                    self._event(type="gemini", model=model, result="429-minute", sec=sec)
                    if attempt < self.max_retries:
                        attempt += 1
                        self._wait(delay or 2, "429")
                        continue
                    _block(model, BUSY_COOLDOWN_SEC)
                    raise _ModelUnavailable("利用上限（1分あたり）") from None
                if code in _RETRYABLE:
                    self._event(type="gemini", model=model, result=str(code), sec=sec)
                    if attempt < self.max_retries:
                        attempt += 1
                        self._log(f"     Gemini が混雑しています（{code}）。少し待って再試行します...")
                        self._wait(1.5, str(code))
                        continue
                    _block(model, BUSY_COOLDOWN_SEC)
                    raise _ModelUnavailable(f"混雑（{code}）") from None
                self._event(type="gemini", model=model, result=str(code), sec=sec)
                raise LLMError(f"Gemini でエラーが発生しました（{code}）: {e.message}") from None
            except Exception as e:  # タイムアウト・通信エラー
                sec = time.perf_counter() - t0
                is_timeout = "Timeout" in type(e).__name__
                self._event(type="gemini", model=model, result="timeout" if is_timeout else "network", sec=sec)
                if is_timeout:
                    _block(model, BUSY_COOLDOWN_SEC)
                    raise _ModelUnavailable(f"{sec:.0f}秒で応答がありませんでした") from None
                if attempt < self.max_retries:
                    attempt += 1
                    self._wait(1.5, "network")
                    continue
                raise LLMError(f"Gemini に接続できませんでした。ネット接続を確認してください。（{e}）") from None

            sec = time.perf_counter() - t0
            u = getattr(res, "usage_metadata", None)
            self._event(
                type="gemini", model=model, result="ok", sec=sec,
                in_tokens=getattr(u, "prompt_token_count", None) or 0,
                out_tokens=getattr(u, "candidates_token_count", None) or 0,
                thought_tokens=getattr(u, "thoughts_token_count", None) or 0,
            )
            result = parse_candidates(res.text)
            if not result:
                raise LLMError("Gemini から空の結果が返ってきました。")
            return result


# リトライする HTTP ステータス（一時的なサーバーエラー）
_RETRYABLE = {408, 500, 502, 503, 504}
