"""VOICEVOX（および VOICEVOX 互換API のエンジン）との通信"""

import copy
from dataclasses import dataclass

import requests

from .errors import EngineError, EngineNotRunningError


@dataclass(frozen=True)
class SpeakerStyle:
    character: str
    style: str
    style_id: int

    @property
    def label(self) -> str:
        return f"{self.character}（{self.style}） [{self.style_id}]"


class VoicevoxClient:
    def __init__(self, base_url: str = "http://127.0.0.1:50021", timeout: float = 60.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            res = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.ConnectionError:
            raise EngineNotRunningError(
                f"音声エンジン（{self.base_url}）に接続できません。"
                "VOICEVOX を起動してから、もう一度実行してください。"
            ) from None
        except requests.Timeout:
            raise EngineError(
                "音声エンジンの応答が時間内に返ってきませんでした。"
                "原稿が長すぎないか、PCの負荷が高すぎないか確認してください。"
            ) from None
        except requests.RequestException as e:
            raise EngineError(f"音声エンジンとの通信に失敗しました: {e}") from None

        if res.status_code == 422:
            raise EngineError(
                "音声エンジンが入力を受け付けませんでした。話者IDが正しいか確認してください。"
                f"（詳細: {res.text[:200]}）"
            )
        if res.status_code != 200:
            raise EngineError(f"音声エンジンがエラーを返しました（{res.status_code}）: {res.text[:200]}")
        return res

    def version(self) -> str:
        return self._request("GET", "/version").json()

    def speakers(self) -> list[SpeakerStyle]:
        data = self._request("GET", "/speakers").json()
        return [
            SpeakerStyle(character=sp["name"], style=st["name"], style_id=st["id"])
            for sp in data
            for st in sp.get("styles", [])
        ]

    def audio_query(self, text: str, speaker: int) -> dict:
        return self._request("POST", "/audio_query", params={"text": text, "speaker": speaker}).json()

    def synthesis(self, query: dict, speaker: int) -> bytes:
        return self._request("POST", "/synthesis", params={"speaker": speaker}, json=query).content


def supports_pause_scale(query: dict) -> bool:
    """句読点の間の長さを変えられるエンジンか（VOICEVOX 0.19 以降など）"""
    return "pauseLengthScale" in query


def with_prosody(query: dict, speed: float, pause_scale: float) -> dict:
    q = copy.deepcopy(query)
    q["speedScale"] = speed
    if supports_pause_scale(q):
        q["pauseLengthScale"] = pause_scale
    return q


def estimate_duration(query: dict) -> float:
    """audio_query の結果から、合成せずに音声の長さ（秒）を見積もる。

    計画を立てるための見積もりなので、最終的な長さは必ず実際の合成結果で確かめる。
    """
    speed = query.get("speedScale", 1.0) or 1.0
    pause_len = query.get("pauseLength")
    pause_scale = query.get("pauseLengthScale", 1.0)

    total = query.get("prePhonemeLength", 0.0) + query.get("postPhonemeLength", 0.0)
    for ap in query.get("accent_phrases", []):
        for m in ap.get("moras", []):
            total += (m.get("consonant_length") or 0.0) + (m.get("vowel_length") or 0.0)
        pm = ap.get("pause_mora")
        if pm:
            length = pause_len if pause_len is not None else (pm.get("vowel_length") or 0.0)
            total += length * pause_scale
    return total / speed
