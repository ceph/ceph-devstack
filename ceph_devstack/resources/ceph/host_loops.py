"""Host loop device discovery and allocation."""

from pathlib import Path


def host_loop_path(number: int) -> str:
    return f"/dev/loop{number}"


def _loop_number_from_backing_name(name: str) -> int | None:
    if "-" not in name:
        return None
    loop_id = name.rsplit("-", 1)[-1]
    if not loop_id.isdigit():
        return None
    return int(loop_id)


def _sysfs_loop_in_use(block_name: str) -> bool:
    block = Path("/sys/class/block") / block_name
    backing = block / "loop" / "backing_file"
    if backing.is_file():
        text = backing.read_text().strip()
        if text not in ("", "(deleted)"):
            return True
    size_file = block / "size"
    if not size_file.is_file():
        return False
    try:
        return int(size_file.read_text()) > 0
    except ValueError:
        return False


def _discover_used_from_sysfs() -> set[int]:
    used: set[int] = set()
    block_dir = Path("/sys/class/block")
    if not block_dir.is_dir():
        return used
    for entry in block_dir.iterdir():
        name = entry.name
        if not name.startswith("loop"):
            continue
        suffix = name.removeprefix("loop")
        if suffix.isdigit() and _sysfs_loop_in_use(name):
            used.add(int(suffix))
    return used


def _discover_used_from_backing_files(image_dir: Path) -> set[int]:
    used: set[int] = set()
    if not image_dir.is_dir():
        return used
    for path in image_dir.iterdir():
        if not path.is_file():
            continue
        if (number := _loop_number_from_backing_name(path.name)) is not None:
            used.add(number)
    return used


def discover_used_loop_numbers(image_dir: Path) -> set[int]:
    """Loop numbers already attached on the host or claimed by backing files."""
    return _discover_used_from_sysfs() | _discover_used_from_backing_files(image_dir)


def owner_loop_numbers(owner: str, image_dir: Path) -> list[int]:
    """Existing backing-file loop numbers for an owner, lowest first."""
    if not image_dir.is_dir():
        return []
    numbers: list[int] = []
    prefix = f"{owner}-"
    for path in image_dir.iterdir():
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        if (number := _loop_number_from_backing_name(path.name)) is not None:
            numbers.append(number)
    return sorted(numbers)


def allocate_loop_numbers(owner: str, count: int, image_dir: Path) -> list[int]:
    """Pick loop numbers for an owner from config count and host availability."""
    used = discover_used_loop_numbers(image_dir)
    reclaimed = owner_loop_numbers(owner, image_dir)
    numbers: list[int] = []
    for index in range(count):
        if index < len(reclaimed):
            numbers.append(reclaimed[index])
            continue
        candidate = 0
        while candidate in used or candidate in numbers:
            candidate += 1
        numbers.append(candidate)
        used.add(candidate)
    return numbers


def allocate_loop_devices(owner: str, count: int, image_dir: Path) -> list[str]:
    return [
        host_loop_path(number)
        for number in allocate_loop_numbers(owner, count, image_dir)
    ]
