"""moOde 音声ボットパッケージ。"""

def main():
    from .main import main as _main
    return _main()

__all__ = ["main"]

