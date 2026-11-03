"""コマンドライン版

  python main.py run 台本.srt -o 出力フォルダ [--speaker 3] [--mode exact|within] [--max-speed 1.3] [--no-rewrite]
  python main.py check      # VOICEVOX と APIキーの診断
  python main.py speakers   # 話者IDの一覧
  python main.py set-key APIキー
"""

import argparse
import sys
from pathlib import Path

from . import APP_NAME, __version__, edition
from .batch import run_job
from .config import default_config_path, load_config, save_config
from .diagnose import diagnose
from .engine import VoicevoxClient
from .errors import TimeFitError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="timefit", description=f"{APP_NAME} {__version__}（{edition.edition_label()}）")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="台本ファイル（CSV/SRT/VTT）を一括で音声にする")
    run.add_argument("input", type=Path)
    run.add_argument("-o", "--output", type=Path, help="出力フォルダ（省略時は 入力ファイル名_output）")
    run.add_argument("--speaker", type=int, help="既定の話者ID（CSVで話者IDを書いていない行・字幕に使う）")
    run.add_argument("--mode", choices=["exact", "within"], help="exact=ちょうど / within=以内")
    run.add_argument("--max-speed", type=float, help="話速の上限（既定 1.3）")
    run.add_argument("--no-rewrite", action="store_true", help="要約を使わない")

    sub.add_parser("check", help="VOICEVOX と APIキーの状態を確認する")
    sub.add_parser("speakers", help="話者IDの一覧を表示する")
    key = sub.add_parser("set-key", help="Gemini の APIキーを保存する")
    key.add_argument("api_key")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config()
        if args.command == "run":
            if args.speaker is not None:
                cfg.default_speaker = args.speaker
            if args.mode:
                cfg.fit_mode = args.mode
            if args.max_speed:
                cfg.max_speed = args.max_speed
            if args.no_rewrite:
                cfg.allow_rewrite = False
            cfg.validate()
            output = args.output or args.input.with_name(args.input.stem + "_output")
            summary = run_job(args.input, output, cfg)
            return 1 if summary.failed else 0
        if args.command == "check":
            ok = True
            for item in diagnose(cfg):
                print(("✅ " if item.ok else "❌ ") + item.message)
                ok = ok and item.ok
            return 0 if ok else 1
        if args.command == "speakers":
            for s in VoicevoxClient(cfg.engine_url).speakers():
                print(s.label)
            return 0
        if args.command == "set-key":
            cfg.gemini_api_key = args.api_key.strip()
            print(f"保存しました: {save_config(cfg)}")
            return 0
    except TimeFitError as e:
        print(f"【エラー】{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
