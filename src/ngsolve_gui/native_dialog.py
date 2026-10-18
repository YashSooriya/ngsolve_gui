"""Backend-side native "open file" dialog returning a real filesystem path.

The frontend runs in a browser whose File System Access API never exposes the
real path of a picked file (by design). Loading a ``.py`` script needs that real
path so it runs with the right ``__file__``/cwd/relative imports, so the load
dialog has to run in the backend process, which does have filesystem access.

We prefer the OS-native picker (``kdialog``/``zenity`` on Linux, ``osascript``
on macOS) for a native look, and fall back to tkinter where none is available.
"""

import os
import shutil
import subprocess
import sys

# ``(label, "ext ext ...")`` groups, mirroring tkinter's filetypes argument.
DEFAULT_FILTERS = [
    ("All Files", "*"),
    ("MM-FEM Problem Files", "*.json *.ngsmodel"),
    ("MM-FEM Mesh Bundles", "*.mmfem-mesh"),
    ("MM-FEM Solution Bundles", "*.mmfem-solutions"),
    ("Mesh Files", "*.vol *.vol.gz"),
    ("Geometry Files", "*.step *.iges *.stp *.brep"),
    ("Python Files", "*.py"),
]


def open_file_dialog(title="Select a file", initialdir=None, filters=DEFAULT_FILTERS):
    """Open a native file-open dialog and return the chosen path, or ``None``."""
    initialdir = initialdir or os.path.expanduser("~")

    if sys.platform == "darwin":
        path = _osascript(title, initialdir)
    elif sys.platform.startswith("linux"):
        path = _kdialog(title, initialdir, filters) or _zenity(title, initialdir, filters)
    elif sys.platform == "win32":
        path = _windows_open_dialog(title, initialdir, filters)
    else:
        path = None

    if path is None:
        path = _tkinter(title, initialdir, filters)
    return path or None


def save_file_dialog(title="Save a file", initialdir=None, filename="", filters=DEFAULT_FILTERS):
    'Open a native save dialog and return the selected filesystem path.'
    initialdir = initialdir or os.path.expanduser("~")
    if sys.platform == "win32":
        return _windows_save_dialog(title, initialdir, filename, filters)

    from tkinter import Tk, filedialog

    root = Tk()
    root.withdraw()
    try:
        return filedialog.asksaveasfilename(
            title=title,
            initialdir=initialdir,
            initialfile=filename,
            filetypes=[(label, patterns) for label, patterns in filters],
            confirmoverwrite=True,
        ) or None
    finally:
        root.destroy()


def _windows_open_dialog(title, initialdir, filters):
    patterns = "|".join(
        part
        for label, group in filters
        for part in (label, ";".join(group.split()) or "*.*")
    ) or "All Files|*.*"
    env = os.environ.copy()
    env.update({
        "MMFEM_DIALOG_TITLE": title,
        "MMFEM_DIALOG_INITIALDIR": initialdir,
        "MMFEM_DIALOG_FILTER": patterns,
    })
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = $env:MMFEM_DIALOG_TITLE
$dialog.Filter = $env:MMFEM_DIALOG_FILTER
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
if (Test-Path -LiteralPath $env:MMFEM_DIALOG_INITIALDIR -PathType Container) {
    $dialog.InitialDirectory = $env:MMFEM_DIALOG_INITIALDIR
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MMFEMOpenWindowApi {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
'@
$ownerHandle = [MMFEMOpenWindowApi]::GetForegroundWindow()
$owner = $null
if ($ownerHandle -ne [IntPtr]::Zero) {
    $owner = New-Object System.Windows.Forms.NativeWindow
    $owner.AssignHandle($ownerHandle)
    $dialogResult = $dialog.ShowDialog($owner)
} else {
    $dialogResult = $dialog.ShowDialog()
}
if ($dialogResult -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.FileName)
}
if ($owner) { $owner.ReleaseHandle() }
$dialog.Dispose()
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
    except OSError as error:
        raise RuntimeError(f"Could not open the Windows file dialog: {error}") from error
    if result.returncode != 0:
        message = result.stderr.strip() or "The Windows file dialog failed."
        raise RuntimeError(message)
    return result.stdout.strip() or None


def _windows_save_dialog(title, initialdir, filename, filters):
    patterns = "|".join(
        part
        for label, group in filters
        for part in (label, ";".join(group.split()) or "*.*")
    ) or "All Files|*.*"
    env = os.environ.copy()
    env.update({
        "MMFEM_DIALOG_TITLE": title,
        "MMFEM_DIALOG_INITIALDIR": initialdir,
        "MMFEM_DIALOG_FILENAME": filename,
        "MMFEM_DIALOG_FILTER": patterns,
    })
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = $env:MMFEM_DIALOG_TITLE
$dialog.FileName = $env:MMFEM_DIALOG_FILENAME
$dialog.Filter = $env:MMFEM_DIALOG_FILTER
$dialog.AddExtension = $true
$dialog.OverwritePrompt = $true
if (Test-Path -LiteralPath $env:MMFEM_DIALOG_INITIALDIR -PathType Container) {
    $dialog.InitialDirectory = $env:MMFEM_DIALOG_INITIALDIR
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MMFEMWindowApi {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
'@
$ownerHandle = [MMFEMWindowApi]::GetForegroundWindow()
$owner = $null
if ($ownerHandle -ne [IntPtr]::Zero) {
    $owner = New-Object System.Windows.Forms.NativeWindow
    $owner.AssignHandle($ownerHandle)
    $dialogResult = $dialog.ShowDialog($owner)
} else {
    $dialogResult = $dialog.ShowDialog()
}
if ($dialogResult -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.FileName)
}
if ($owner) { $owner.ReleaseHandle() }
$dialog.Dispose()
'''
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
    except OSError as error:
        raise RuntimeError(f"Could not open the Windows save dialog: {error}") from error
    if result.returncode != 0:
        message = result.stderr.strip() or "The Windows save dialog failed."
        raise RuntimeError(message)
    return result.stdout.strip() or None


def _run(cmd):
    """Run a dialog command; return stripped stdout, or None on cancel/error."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if out.returncode != 0:
        return None  # non-zero == user cancelled (or tool missing)
    return out.stdout.strip() or None


def _kdialog(title, initialdir, filters):
    if not shutil.which("kdialog"):
        return None
    # kdialog filter syntax: "patterns|Label" entries joined by newlines.
    flt = "\n".join(f"{pats}|{label}" for label, pats in filters)
    return _run(["kdialog", "--title", title, "--getopenfilename", initialdir + "/", flt])


def _zenity(title, initialdir, filters):
    if not shutil.which("zenity"):
        return None
    cmd = ["zenity", "--file-selection", "--title", title,
           "--filename", initialdir + "/"]
    for label, pats in filters:
        cmd += ["--file-filter", f"{label} | {pats}"]
    return _run(cmd)


def _osascript(title, initialdir):
    script = (
        f'POSIX path of (choose file with prompt "{title}" '
        f'default location (POSIX file "{initialdir}"))'
    )
    return _run(["osascript", "-e", script])


def _tkinter(title, initialdir, filters):
    from tkinter import Tk, filedialog

    root = Tk()
    root.withdraw()
    try:
        return filedialog.askopenfilename(
            title=title,
            initialdir=initialdir,
            filetypes=[(label, pats) for label, pats in filters],
        )
    finally:
        root.destroy()
