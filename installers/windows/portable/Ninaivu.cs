// Ninaivu.exe -- the one program at the top of the portable zip.
//
// The zip holds this file, an app folder and a logs folder, and nothing else
// where a person can see it. Everything Ninaivu is made of (its own Python,
// its packages, the index of the library, the AI models) is inside app\, so
// the whole thing can be copied to another disk, or onto a USB drive, as one
// folder. The log of what it did is in logs\, where somebody who is asked for
// it can find it without looking.
//
// This starts the Ninaivu Control Panel, as the Start menu entry of the
// installed Ninaivu does, with the folders it needs told to it through the
// environment, and then ends. It has no window of its own; if something is
// wrong it says so in one message box and ends.
//
// Built by installers\windows\build-portable.ps1 with the csc.exe that ships
// with Windows, so building it needs nothing installed. Windows 10 and 11
// carry the .NET Framework it runs on.

using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr window, string text, string caption, uint type);

    private const uint MB_OK_ICONERROR = 0x10;

    private static void Say(string text)
    {
        MessageBoxW(IntPtr.Zero, text, "Ninaivu", MB_OK_ICONERROR);
    }

    // One argument as the Windows command line (CommandLineToArgvW) reads it
    // back: backslashes are literal except in front of a quote, where they are
    // doubled, so C:\a b and \\nas\share arrive as they were typed.
    private static string Quote(string argument)
    {
        if (argument.Length > 0 && argument.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return argument;
        var quoted = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in argument)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"') quoted.Append('\\', backslashes * 2 + 1);
            else quoted.Append('\\', backslashes);
            backslashes = 0;
            quoted.Append(c);
        }
        quoted.Append('\\', backslashes * 2);
        return quoted.Append('"').ToString();
    }

    private static int Main(string[] args)
    {
        // The folder this program is in, as a full path and not trimmed:
        // trimmed, the root of a drive (E:\) becomes "E:", which means "the
        // current folder on E:" and not its root, and every path made from it
        // points somewhere else. Path.Combine copes with a trailing slash.
        string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
        string app = Path.Combine(root, "app");
        string python = Path.Combine(app, "python", "pythonw.exe");

        if (!File.Exists(python))
        {
            Say("Ninaivu's program files were not found next to Ninaivu.exe.\n\n"
                + "If you opened Ninaivu.exe from inside the zip file, close it, "
                + "right-click the zip and choose Extract All, and open Ninaivu.exe "
                + "from the folder it makes.");
            return 1;
        }

        string logs = Path.Combine(root, "logs");
        string data = Path.Combine(app, "data");
        try
        {
            Directory.CreateDirectory(logs);
            Directory.CreateDirectory(data);
        }
        catch (Exception exc)
        {
            Say("Ninaivu cannot write to the folder it is in:\n\n" + root + "\n\n"
                + "Move the whole folder somewhere you can write to, such as your "
                + "Documents or a USB drive, and open Ninaivu.exe there.\n\n" + exc.Message);
            return 1;
        }

        // "Start when I sign in" is registered as `Ninaivu.exe --autostart`:
        // a sign-in has no environment of its own, so the server is started
        // from here, with the same folders, and not by the registry entry
        // directly. It is what the installed Ninaivu's entry does, minus the
        // environment variable the installer writes for it.
        var arguments = new StringBuilder();
        if (args.Length == 1 && args[0] == "--autostart")
        {
            arguments.Append("-m ninaivu.desktop.autostart --start");
        }
        else
        {
            arguments.Append("-m ninaivu.desktop.app");
            foreach (string argument in args) arguments.Append(' ').Append(Quote(argument));
        }

        var start = new ProcessStartInfo(python, arguments.ToString())
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = app,
        };
        // What makes this the portable build: where Ninaivu keeps the files
        // beside itself, its index and settings, the AI models and its logs.
        start.EnvironmentVariables["NINAIVU_HOME"] = app;
        start.EnvironmentVariables["NINAIVU_STATE_DIR"] = data;
        start.EnvironmentVariables["NINAIVU_AI_MODELS_DIR"] = Path.Combine(app, "ai-models");
        start.EnvironmentVariables["NINAIVU_LOG_DIR"] = logs;
        start.EnvironmentVariables["NINAIVU_PORTABLE"] = "1";
        // This very program, whatever it has been renamed to, so "Start when
        // I sign in" registers it and not a guess at its name.
        start.EnvironmentVariables["NINAIVU_LAUNCHER"] = Process.GetCurrentProcess().MainModule.FileName;
        // The private Python must not be steered by another one's settings.
        start.EnvironmentVariables.Remove("PYTHONHOME");
        start.EnvironmentVariables.Remove("PYTHONPATH");

        try
        {
            Process.Start(start);
        }
        catch (Exception exc)
        {
            Say("Ninaivu could not be started.\n\n" + exc.Message);
            return 1;
        }
        return 0;
    }
}
