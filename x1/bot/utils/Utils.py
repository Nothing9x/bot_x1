import asyncio


async def stop_task(task: asyncio.Task | None):
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            print(f"Task {task.get_name() if hasattr(task, 'get_name') else ''} was cancelled.")

def get_proxies(proxy: str | None):
    if not proxy:
        return None
    p = proxy.strip()
    if p.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return {
            "http": p,
            "https": p,
        }
    return {"http": f"http://{p}", "https": f"http://{p}"}

def get_proxies_for_ws(proxy: str):
    if proxy == "":
        return None
    p = proxy.strip()
    if p.startswith("socks5h://"):
        return "socks5://" + p[len("socks5h://"):]
    if p.startswith(("socks5://", "http://", "https://")):
        return p
    return f"http://{p}"
