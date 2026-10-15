"""Daily, local-only Weixin chat export. Run with --check for read-only preflight."""
from __future__ import annotations
import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import uuid
import webbrowser
import winreg
import msvcrt
from selection import require_full_capture

BASE=Path(__file__).resolve().parent

def _private_base():
    # Renamed project: keep a private root an earlier name already filled, so an
    # existing index and its captures are never orphaned by the rename.
    parent=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))
    for name in ('dsh-better-wechat','dsh-wechat-assistant'):
        if (parent/name).is_dir():
            return parent/name
    return parent/'dsh-better-wechat'

PRIVATE_BASE=_private_base()
TZ=timezone(timedelta(hours=8))
DEFAULTS={"account_dir":"","data_root":"","output_root":str(PRIVATE_BASE/'capture-output'),"open_after_export":True,"compress_jsonl":True,"selection":None}
PS_QUERY=r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$items = @(Get-CimInstance Win32_Process -Filter "Name='Weixin.exe'" | ForEach-Object {
  $version = $null
  if ($_.ExecutablePath) { $version = (Get-Item -LiteralPath $_.ExecutablePath).VersionInfo.FileVersion }
  [ordered]@{pid=$_.ProcessId; image=$_.ExecutablePath; command=$_.CommandLine; version=$version}
})
ConvertTo-Json -InputObject $items -Compress
"""

class DailyError(RuntimeError): pass
class AlreadyRunning(DailyError): pass

def now(): return datetime.now(TZ)

def write_json(path,value):
    temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex[:8])
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    os.replace(temp,path)

def read_config(path):
    result=dict(DEFAULTS)
    if path.exists():
        value=json.loads(path.read_text('utf-8-sig'))
        if not isinstance(value,dict): raise DailyError('config.json 必须是 JSON 对象。')
        unknown=set(value)-set(DEFAULTS)
        if unknown: raise DailyError('配置项无法识别：'+', '.join(sorted(unknown)))
        result.update(value)
    for name in ('account_dir','data_root','output_root'):
        if not isinstance(result[name],str): raise DailyError(name+' 必须是路径字符串。')
    for name in ('open_after_export','compress_jsonl'):
        if not isinstance(result[name],bool): raise DailyError(name+' 必须是 true 或 false。')
    result['selection']=require_full_capture(result['selection'])
    return result

def resolve_path(value):
    p=Path(os.path.expandvars(value)).expanduser()
    return (BASE/p).resolve() if not p.is_absolute() else p.resolve()

def documents_folder():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            raw,_=winreg.QueryValueEx(key,'Personal')
        return Path(os.path.expandvars(raw))
    except OSError: return Path.home()/'Documents'

def discover_accounts(config):
    if config['account_dir']:
        account=resolve_path(config['account_dir'])
        if not (account/'db_storage').is_dir(): raise DailyError('指定账号目录下没有 db_storage：'+str(account))
        return [account]
    roots=[resolve_path(config['data_root'])] if config['data_root'] else [documents_folder()/'xwechat_files',Path.home()/'Documents'/'xwechat_files']
    if not config['data_root']:
        for keyname in (r'Software\Tencent\Weixin',r'Software\Tencent\WeChat'):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,keyname) as key:
                    value,_=winreg.QueryValueEx(key,'FileSavePath')
                if isinstance(value,str) and value and value!='MyDocument:':
                    folder=Path(os.path.expandvars(value)); roots.extend([folder,folder/'xwechat_files'])
            except OSError: pass
    accounts=set()
    for root in roots:
        if not root.is_dir(): continue
        if (root/'db_storage').is_dir(): accounts.add(root.resolve())
        for child in root.iterdir():
            if child.is_dir() and (child/'db_storage').is_dir(): accounts.add(child.resolve())
    return sorted(accounts,key=str)

def discover_processes():
    try:
        completed=subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',PS_QUERY],capture_output=True,encoding='utf8',errors='strict',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise DailyError('无法读取微信进程列表：'+str(exc)) from exc
    if completed.returncode: raise DailyError('读取微信进程列表失败。请确认当前 Windows 用户可以访问微信进程。')
    try: value=json.loads(completed.stdout.strip() or '[]')
    except json.JSONDecodeError as exc: raise DailyError('微信进程列表返回格式异常。') from exc
    if isinstance(value,dict): value=[value]
    found=[]
    for item in value:
        command=item.get('command') or ''
        image=item.get('image')
        if image and Path(image).name.lower()=='weixin.exe' and '--type=' not in command:
            found.append({'pid':int(item['pid']),'image':str(Path(image).resolve()),'version':item.get('version')})
    return sorted(found,key=lambda item:item['pid'])

def dependencies():
    if struct.calcsize('P')!=8 or sys.version_info<(3,10): raise DailyError('需要 64 位 Python 3.10 或更新版本。')
    versions={}
    for package in ('cryptography','zstandard'):
        try: versions[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise DailyError('缺少依赖 '+package+'。请先双击“安装依赖.cmd”，然后再次运行。')
    return versions

def pick_account(accounts):
    if not accounts: raise DailyError('没有找到本地微信 4.x 数据。请先登录微信，或在 config.json 设置 account_dir（含 db_storage 的账号文件夹）。')
    if len(accounts)>1:
        raise DailyError('检测到多个账号，请在 config.json 的 account_dir 指定本次账号，避免导错：\n'+'\n'.join(str(p) for p in accounts))
    return accounts[0]

def estimate_bytes(account):
    total=0
    for path in (account/'db_storage').rglob('*'):
        if path.is_file() and (path.name.endswith('.db') or path.name.endswith('.db-wal')):
            total+=path.stat().st_size
    return max(512*1024*1024,total*4)

def validate_output(output,account):
    if output==account or account in output.parents: raise DailyError('输出目录不能位于微信账号数据目录内。')
    existing=output
    while not existing.exists(): existing=existing.parent
    need=estimate_bytes(account); free=shutil.disk_usage(existing).free
    if free<need: raise DailyError('可用空间不足。建议至少保留 '+str(round(need/1024**3,2))+' GB。')
    return need,free

@contextmanager
def lock_output(root):
    root.mkdir(parents=True,exist_ok=True)
    path=root/'.daily-export.lock'
    lock=path.open('a+b')
    acquired=False
    try:
        lock.seek(0,2)
        if not lock.tell(): lock.write(b'0'); lock.flush()
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1); acquired=True
        except OSError: raise AlreadyRunning('已有提取任务在运行，请等待当前任务结束后重试。')
        yield
    finally:
        if acquired:
            lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
        lock.close()

class Tee:
    def __init__(self,terminal,log): self.terminal=terminal; self.log=log
    def write(self,text):
        self.terminal.write(text); self.log.write(text); self.flush(); return len(text)
    def flush(self): self.terminal.flush(); self.log.flush()

def run_once(config,account,processes,dep_versions,output,open_result):
    require_full_capture(config.get('selection'))
    output.mkdir(parents=True,exist_ok=True)
    run_id=now().strftime('%Y-%m-%d_%H-%M-%S')+'_'+uuid.uuid4().hex[:6]
    run_dir=output/now().strftime('%Y-%m-%d')/run_id
    run_dir.parent.mkdir(parents=True,exist_ok=True)
    logs=output/'logs'; logs.mkdir(exist_ok=True)
    log_path=logs/(run_id+'.log')
    status={'run_id':run_id,'started_at':now().isoformat(),'status':'running','account_dir':str(account),'output_dir':str(run_dir),'dependencies':dep_versions,'wechat_versions':sorted({p.get('version') or 'unknown' for p in processes}),'timezone':'Asia/Shanghai','mode':'full_snapshot','log':str(log_path)}
    status_path=logs/(run_id+'.json')
    write_json(status_path,status)
    started=time.monotonic()
    published=False
    try:
        with log_path.open('w',encoding='utf8',newline='\n') as logfile, redirect_stdout(Tee(sys.stdout,logfile)):
            print('微信聊天记录每日提取')
            print('本次结果：'+str(run_dir))
            print('1/3 读取微信数据库与增量日志的稳定副本，校验后解密。')
            from extract_local import capture_account
            report=capture_account(account,run_dir,processes,progress=print)
            if report.get('status')!='verified': raise DailyError('数据库验证未全部通过，停止导出。')
            print('2/3 导出正文、会话文本和离线阅读页面。')
            from export_records import export_capture
            summary=export_capture(run_dir,compress_jsonl=config['compress_jsonl'])
            status['summary']=summary
            state=summary.get('status','complete')
            if state not in ('complete','complete_with_warnings'): raise DailyError('导出未完整结束，请查看校验报告。')
            status.update(status=state,completed_at=now().isoformat(),elapsed_seconds=round(time.monotonic()-started,2))
            write_json(run_dir/'run_report.json',status)
            write_json(status_path,status)
            index=run_dir/'export'/'index.html'
            if not index.is_file(): raise DailyError('离线阅读页面没有生成。')
            if state in ('complete','complete_with_warnings'):
                latest={'run_id':run_id,'completed_at':status['completed_at'],'account_dir':str(account),'run_dir':str(run_dir),'index':str(index),'messages':summary['messages'],'conversations':summary['conversations'],'status':state,'warnings':summary.get('warnings',[])}
                account_hash=hashlib.sha256(str(account).casefold().encode('utf8')).hexdigest()[:16]
                pointer=output/'latest.json'
                old_pointer=json.loads(pointer.read_text('utf8')) if pointer.exists() else {}
                by_account=old_pointer.get('accounts',{})
                if not isinstance(by_account,dict): raise DailyError('latest.json account index is invalid.')
                by_account[account_hash]=dict(latest)
                latest['accounts']=by_account
                write_json(pointer,latest)
                published=True
            print('3/3 核对完成：'+str(summary['messages'])+' 条消息，'+str(summary['conversations'])+' 个会话。')
            print('阅读页面：'+str(index))
            if state=='complete_with_warnings':
                print('原始记录已完整保存；部分显示有警告，请查看 export_summary.json。最新结果已更新。')
            else: print('提取成功。历史结果已保留。')
        if open_result:
            try: webbrowser.open(index.as_uri())
            except Exception:
                try: print('导出已完成；浏览器未能自动打开，请手动打开上述 index.html。')
                except Exception: pass
        return 0 if state=='complete' else 2
    except Exception as exc:
        if published:
            try:
                with log_path.open('a',encoding='utf8') as logfile: logfile.write('\n导出已经完成；后续提示失败：'+type(exc).__name__+'\n')
            except Exception: pass
            return 0 if status.get('status')=='complete' else 2
        status.update(status='failed',completed_at=now().isoformat(),elapsed_seconds=round(time.monotonic()-started,2),error=type(exc).__name__+': '+str(exc))
        write_json(status_path,status)
        if run_dir.exists(): write_json(run_dir/'run_report.json',status)
        with log_path.open('a',encoding='utf8') as logfile: logfile.write('\n失败：'+status['error']+'\n')
        print('提取未完成：'+str(exc),file=sys.stderr)
        print('失败报告：'+str(status_path),file=sys.stderr)
        print('上一次成功结果未被替换。',file=sys.stderr)
        return 1

def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf8')
    if hasattr(sys.stderr,'reconfigure'): sys.stderr.reconfigure(encoding='utf8')
    parser=argparse.ArgumentParser(description='微信 4.x 本地聊天记录每日提取；每次全量保存到新目录。')
    parser.add_argument('--config',type=Path,default=PRIVATE_BASE/'collector-config.json')
    parser.add_argument('--account',help='账号目录（其下应有 db_storage）')
    parser.add_argument('--data-root',help='xwechat_files 目录')
    parser.add_argument('--output',help='结果根目录')
    parser.add_argument('--check',action='store_true',help='只检查环境，不读取进程内存或导出聊天')
    parser.add_argument('--selection-file',type=Path,help=argparse.SUPPRESS)
    opening=parser.add_mutually_exclusive_group()
    opening.add_argument('--open',dest='open_result',action='store_true')
    opening.add_argument('--no-open',dest='open_result',action='store_false')
    parser.set_defaults(open_result=None)
    args=parser.parse_args(argv)
    try:
        config=read_config(args.config)
        if args.selection_file is not None:
            with args.selection_file.open('r',encoding='utf-8-sig') as stream:
                text=stream.read(65537)
            if len(text.encode('utf-8'))>65536: raise DailyError('范围配置过大。')
            config['selection']=require_full_capture(json.loads(text))
        for cli,key in ((args.account,'account_dir'),(args.data_root,'data_root'),(args.output,'output_root')):
            if cli is not None: config[key]=cli
        deps=dependencies(); account=pick_account(discover_accounts(config)); processes=discover_processes()
        if not processes: raise DailyError('请先打开并登录微信 4.x，再运行每日提取。当前未发现可读的 Weixin.exe 主进程。')
        output=resolve_path(config['output_root'] or DEFAULTS['output_root'])
        need,free=validate_output(output,account)
        if args.check:
            print(json.dumps({'status':'preflight_ok','account_dir':str(account),'output_root':str(output),'processes':processes,'dependencies':deps,'estimated_required_gb':round(need/1024**3,2),'free_gb':round(free/1024**3,2),'note':'环境检查通过；真正解密仍需密钥认证与数据库校验。'},ensure_ascii=False,indent=2))
            return 0
        open_result=config['open_after_export'] if args.open_result is None else args.open_result
        with lock_output(output): return run_once(config,account,processes,deps,output,open_result)
    except (DailyError,OSError,ValueError) as exc:
        print('无法开始提取：'+str(exc),file=sys.stderr); return 1

if __name__=='__main__': raise SystemExit(main())
