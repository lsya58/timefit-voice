"""TimeFit Voice の起動口

  python main.py                 → 画面（GUI）を開く
  python main.py run 台本.srt    → コマンドラインで一括処理（python main.py --help で一覧）
"""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from timefit.cli import main as cli_main

        return cli_main()
    from timefit.gui import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
