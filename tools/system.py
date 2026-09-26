import psutil


def get_system_status() -> str:
    cpu = psutil.cpu_percent(interval=1)

    memory = psutil.virtual_memory()

    used_gb = memory.used / (1024 ** 3)
    total_gb = memory.total / (1024 ** 3)

    return (
        f"CPU usage: {cpu:.1f}%\n"
        f"RAM usage: {used_gb:.1f} GB / {total_gb:.1f} GB\n"
        f"RAM usage percentage: {memory.percent:.1f}%"
    )
