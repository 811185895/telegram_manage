"""Kill claude-profile users briefly, copy profile to --script, restart MCPs naturally."""
import os, shutil, subprocess, time, sys

SRC = r"D:\UserData\.playwright_user_data--claude"
DST = r"D:\UserData\.playwright_user_data--script"


def query_holders():
    r = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Get-CimInstance Win32_Process | ? { $_.CommandLine -like "*playwright_user_data--claude*" } | Select-Object -ExpandProperty ProcessId'],
        capture_output=True, text=True
    )
    return [int(x) for x in r.stdout.strip().split('\n') if x.strip().isdigit()]


def kill_all():
    pids = query_holders()
    for pid in pids:
        subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, timeout=5)
    return pids


def main():
    if os.path.exists(DST):
        print(f"removing existing {DST}")
        shutil.rmtree(DST, ignore_errors=True)

    # Kill repeatedly until count stays at 0 for 1 second
    for attempt in range(10):
        killed = kill_all()
        print(f"attempt {attempt}: killed {len(killed)}")
        time.sleep(0.6)
        remaining = query_holders()
        if not remaining:
            # second check after delay
            time.sleep(0.3)
            if not query_holders():
                break

    # Try to copy quickly
    print("copying profile...")
    try:
        shutil.copytree(SRC, DST, dirs_exist_ok=False, ignore=shutil.ignore_patterns(
            'BrowserMetrics', 'DeferredBrowserMetrics', 'Crashpad', 'ShaderCache', 'GraphiteDawnCache', 'GrShaderCache',
        ))
    except FileExistsError:
        shutil.rmtree(DST, ignore_errors=True)
        shutil.copytree(SRC, DST, dirs_exist_ok=False)
    print("done. DST contents:", len(os.listdir(DST)))


if __name__ == "__main__":
    main()
