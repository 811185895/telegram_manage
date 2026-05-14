import os, subprocess, time

PROFILE = r"D:\UserData\.playwright_user_data--script"

def query():
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq "chrome.exe" -or $_.Name -eq "msedge.exe" -or $_.Name -eq "python.exe") -and $_.CommandLine -like "*playwright_user_data--script*" } | Select-Object -ExpandProperty ProcessId'],
        capture_output=True, text=True
    )
    return [int(x) for x in r.stdout.strip().split('\n') if x.strip().isdigit()]

pids = query()
print(f"killing {len(pids)}:", pids)
for pid in pids:
    subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
time.sleep(2)
remaining = query()
print(f"remaining {len(remaining)}:", remaining)
for f in ('lockfile','SingletonLock','SingletonCookie','SingletonSocket'):
    p = os.path.join(PROFILE, f)
    try:
        os.remove(p)
        print('removed', p)
    except FileNotFoundError:
        pass
    except Exception as e:
        print('cant', p, e)
