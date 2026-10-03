Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\marti\Documents\AntiG\AlexaPC\Brillo"
WshShell.Run "cmd /c pythonw brillo_main.py", 0, False