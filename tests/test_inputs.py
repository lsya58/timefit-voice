import pytest

from timefit.errors import InputFileError
from timefit.inputs import load_script, parse_csv, parse_subtitles

SRT = """1
00:00:01,000 --> 00:00:03,500
<i>こんにちは</i>
ずんだもんなのだ

2
00:00:04,000 --> 00:00:06,000
今日は江戸時代の話をするのだ
"""


def test_parse_srt():
    lines = parse_subtitles(SRT.replace("\n", "\r\n"))
    assert len(lines) == 2
    assert lines[0].text == "こんにちはずんだもんなのだ"
    assert lines[0].start_sec == 1.0
    assert lines[0].target_sec == pytest.approx(2.5)
    assert lines[1].index == 2


def test_parse_vtt_without_hours():
    vtt = "WEBVTT\n\nNOTE メモ\n\n00:01.000 --> 00:02.500\nテスト\n"
    lines = parse_subtitles(vtt)
    assert len(lines) == 1
    assert lines[0].target_sec == pytest.approx(1.5)


def test_srt_with_reversed_time_is_rejected():
    with pytest.raises(InputFileError):
        parse_subtitles("1\n00:00:03,000 --> 00:00:01,000\nだめ\n")


def test_csv_without_header():
    lines = parse_csv("3,10,江戸時代、徳川家康は幕府を開いた\n2,5.5,こんにちは\n")
    assert [(l.speaker_id, l.target_sec) for l in lines] == [(3, 10.0), (2, 5.5)]
    assert lines[0].text == "江戸時代、徳川家康は幕府を開いた"  # 引用符なしのカンマも原稿の一部として扱う


def test_csv_with_japanese_header_and_two_columns():
    lines = parse_csv("秒数,原稿\n# コメント行\n\n4,テスト\n")
    assert len(lines) == 1
    assert lines[0].speaker_id is None


def test_csv_error_has_line_number():
    with pytest.raises(InputFileError, match="2行目"):
        parse_csv("3,10,OK\n3,abc,NG\n")


def test_cp932_csv_file(tmp_path):
    p = tmp_path / "script.csv"
    p.write_bytes("話者ID,秒数,原稿\n3,5,ずんだもんなのだ\n".encode("cp932"))
    script = load_script(p)
    assert script.kind == "csv"
    assert script.lines[0].text == "ずんだもんなのだ"


def test_unknown_extension(tmp_path):
    p = tmp_path / "a.docx"
    p.write_text("x")
    with pytest.raises(InputFileError):
        load_script(p)
