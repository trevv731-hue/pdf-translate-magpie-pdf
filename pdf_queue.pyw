import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def main():
    args = sys.argv[1:]
    # 如果传了参数（例如拖拽了 PDF），或者传了子命令（run/list/reset 等），走原有队列翻译逻辑
    if args:
        bat = os.path.join(HERE, "pdfq.bat")
        joined = " ".join('"%s"' % a for a in args)
        cmd = 'cmd /c ""%s" %s"' % (bat, joined)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(cmd, cwd=HERE, creationflags=flags)
        return 0

    # 双击（无参数）直接打开交互式选择模型/引擎菜单
    menu_bat = os.path.join(HERE, "选择模型.bat")
    if os.path.isfile(menu_bat):
        os.startfile(menu_bat)
        return 0

    bat = os.path.join(HERE, "pdfq.bat")
    os.startfile(bat)
    return 0

if __name__ == "__main__":
    sys.exit(main())
