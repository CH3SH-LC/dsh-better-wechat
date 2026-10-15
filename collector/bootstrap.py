"""Select an installed 64-bit Python that has the required local dependencies."""
import argparse,os,shutil,subprocess,sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
HIDDEN=getattr(subprocess,'CREATE_NO_WINDOW',0)
def candidates():
    values=[BASE/'.venv'/'Scripts'/'python.exe',Path(sys.executable)]
    command=shutil.which('python')
    if command and 'windowsapps' not in command.lower(): values.append(Path(command))
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive,r'Software\Python\PythonCore',0,winreg.KEY_READ|view) as root:
                        versions=[winreg.EnumKey(root,i) for i in range(winreg.QueryInfoKey(root)[0])]
                    for version in versions:
                        try:
                            with winreg.OpenKey(hive,r'Software\Python\PythonCore'+'\\'+version+r'\InstallPath',0,winreg.KEY_READ|view) as key:
                                try: value,_=winreg.QueryValueEx(key,'ExecutablePath')
                                except OSError: value=str(Path(winreg.QueryValue(key,None))/'python.exe')
                            values.append(Path(value))
                        except OSError: pass
                except OSError: pass
    except ImportError: pass
    seen=set()
    for value in values:
        ident=str(value).casefold()
        if value.is_file() and ident not in seen:
            seen.add(ident); yield value
def choose_python():
    probe="import sys,struct,cryptography,zstandard;sys.exit(0 if struct.calcsize('P')==8 and sys.version_info>=(3,10) else 1)"
    for candidate in candidates():
        try:
            result=subprocess.run([str(candidate),'-X','utf8','-c',probe],capture_output=True,timeout=20,creationflags=HIDDEN)
            if result.returncode==0: return candidate
        except (OSError,subprocess.TimeoutExpired): pass
    raise RuntimeError('No compatible Python with dependencies. Run the dependency installer first.')
def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('daily','latest'))
    args,extra=parser.parse_known_args(argv)
    try:
        python=choose_python() if args.mode=='daily' else Path(sys.executable)
        target=BASE/('daily_export.py' if args.mode=='daily' else 'open_latest.py')
        return subprocess.call([str(python),'-X','utf8',str(target),*extra],cwd=BASE)
    except (OSError,RuntimeError) as exc:
        print(str(exc),file=sys.stderr); return 1
if __name__=='__main__': raise SystemExit(main())
