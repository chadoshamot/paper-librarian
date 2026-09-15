"""PaperBook 桌面壳：把 web 界面套进原生窗口，双击打开、无需终端。

实现：复用 service.web 的 ThreadingHTTPServer 在后台线程起本地服务，
再用 pywebview 开一个原生窗口加载本地地址。窗口关闭即优雅停服。
若未装 pywebview（或无可用的 WebView 后端），回退为用默认浏览器打开。
"""
import socket
import threading


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    from . import web as webmod

    port = _free_port()
    server = webmod.create_server("127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"

    try:
        import webview  # noqa: WPS433
    except Exception:
        webview = None

    if webview is None:
        _fallback_browser(url, server)
        return

    try:
        window = webview.create_window(
            "PaperBook", url, width=1280, height=840,
            min_size=(960, 640), text_select=False,
        )
        webview.start()
        _ = window
    finally:
        server.shutdown()


def _fallback_browser(url: str, server) -> None:
    import webbrowser

    webbrowser.open(url)
    print(f"[PaperBook] 未检测到 pywebview/WebView2 后端，已用默认浏览器打开 {url}")
    print("          保持本窗口运行，关闭它即停止服务（Ctrl+C 退出）。")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
