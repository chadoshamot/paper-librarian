"""PaperBook 桌面启动器（双击打开，无终端黑窗）。"""
import ctypes
import sys
import traceback


def _fatal(msg: str) -> None:
    # 无控制台环境下用原生对话框提示，避免静默失败。
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "PaperBook 启动失败", 0x10)
    except Exception:
        sys.stderr.write(msg + "\n")


if __name__ == "__main__":
    try:
        from service.desktop import main
        main()
    except SystemExit:
        raise
    except Exception:
        _fatal(traceback.format_exc())
