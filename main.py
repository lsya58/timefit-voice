import os
import wave
import requests
import google.generativeai as genai
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# 実行環境はPowerShell(Windows)なので、ローカルホスト指定で完璧に動きます
VOICEVOX_URL = "http://127.0.0.1:50021"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("【エラー】GEMINI_API_KEY が .env に設定されていません。")
    exit(1)

# Geminiの設定
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

def generate_voicevox_audio(text, speaker_id, speed_scale=1.0):
    """VOICEVOXにテキストを送り、指定した話速で音声バイナリを生成する"""
    res_query = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": speaker_id}
    )
    query_data = res_query.json()
    query_data["speedScale"] = speed_scale

    res_synth = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        json=query_data
    )
    return res_synth.content

def get_wav_duration(wav_bytes):
    """wavバイナリから正確な再生時間（秒）を計算する"""
    with open("temp.wav", "wb") as f:
        f.write(wav_bytes)
    
    with wave.open("temp.wav", "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        duration = frames / float(rate)
    return duration

def rewrite_text_by_gemini(original_text, target_sec):
    """Geminiを使って目標秒数に収まるよう原稿をリライトする"""
    target_length = int(target_sec * 7) # 1秒あたり約7文字目安
    prompt = (
        f"以下の原稿を、元の意味や重要な固有名詞を保ったまま、"
        f"約{target_length}文字の自然な話し言葉に要約してください。\n\n"
        f"【原稿】\n{original_text}"
    )
    response = model.generate_content(prompt)
    return response.text.strip()

def fit_audio_to_target(text, speaker_id, target_sec, output_filename="output.wav"):
    """尺合わせのメインロジック"""
    print(f"\n【開始】目標秒数: {target_sec}秒")
    print(f"【元の原稿】{text}\n")
    
    # 1. まずは標準話速（1.0倍）で生成して計測
    wav_bytes = generate_voicevox_audio(text, speaker_id, speed_scale=1.0)
    current_sec = get_wav_duration(wav_bytes)
    print(f"標準話速での秒数: {current_sec:.2f}秒")

    if current_sec <= target_sec:
        print("✅ 目標秒数内に収まりました（等倍）。")
        with open(output_filename, "wb") as f:
            f.write(wav_bytes)
        return

    # 2. 収まらない場合、少し早口（最大1.3倍）にして収まるか判定
    required_speed = current_sec / target_sec
    if required_speed <= 1.3:
        print(f"🔄 話速調整で対応可能と判断（{required_speed:.2f}倍）。生成中...")
        wav_bytes = generate_voicevox_audio(text, speaker_id, speed_scale=required_speed)
        final_sec = get_wav_duration(wav_bytes)
        print(f"✅ 話速調整後の秒数: {final_sec:.2f}秒")
        with open(output_filename, "wb") as f:
            f.write(wav_bytes)
        return

    # 3. 1.3倍でも収まらない（早口すぎる）場合は、Geminiで台本を短くする
    print("⚠️ 話速調整(1.3倍)では不自然になるため、Geminiで台本をリライトします...")
    rewritten_text = rewrite_text_by_gemini(text, target_sec)
    print(f"\n【リライト後原稿】\n{rewritten_text}\n")

    # 4. リライト後のテキストで再生成（等倍で試す）
    wav_bytes = generate_voicevox_audio(rewritten_text, speaker_id, speed_scale=1.0)
    final_sec = get_wav_duration(wav_bytes)
    
    # わずかに超える場合は微調整
    if final_sec > target_sec:
        fine_tune_speed = final_sec / target_sec
        print(f"🔄 リライト後、微調整のため話速を {fine_tune_speed:.2f}倍 に設定します。")
        wav_bytes = generate_voicevox_audio(rewritten_text, speaker_id, speed_scale=fine_tune_speed)
        final_sec = get_wav_duration(wav_bytes)

    print(f"✅ 最終生成秒数: {final_sec:.2f}秒")
    with open(output_filename, "wb") as f:
        f.write(wav_bytes)

if __name__ == "__main__":
    # テストデータ（ずんだもん: speaker=3）
    test_text = "江戸時代、徳川家康は江戸幕府を開き、260年以上にわたる平和な時代、いわゆるパクス・トクガワーナを築き上げました。この時代には町人文化が大きく花開き、浮世絵や歌舞伎などが発展しました。"
    target_seconds = 10.0
    speaker = 3
    
    fit_audio_to_target(test_text, speaker, target_seconds)