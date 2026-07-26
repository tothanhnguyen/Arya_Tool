"""Wizard noi API key hang AI: `python -m laplace.llm.setup` (T15).

Luong: liet ke hang danh so -> chon -> dan key qua getpass (khong echo,
KHONG nhan key qua tham so CLI de khoi lot shell history) -> goi 1 request
nho validate key song (in latency + model tra loi) -> moi ghi `.env`:
sua DUNG dong (giu nguyen comment + cac dong khac), key da co thi hoi truoc
khi de, backup `.env.bak`, chmod 600 ca hai; dat luon LAPLACE_LLM_PROVIDER.

Bao mat: key khong bao gio duoc in day du — moi cho hien thi deu qua
mask_key() (6 ky tu dau + "..."); key chi duoc ghi vao .env, khong file khac.
"""

import getpass
import os
import sys
import time
from pathlib import Path

from laplace.llm.presets import PRESETS, ProviderPreset, mask_key

# .env o goc repo — cung duong dan tuyet doi voi laplace/config.py
ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
ENV_EXAMPLE_PATH = ENV_PATH.with_name(".env.example")


# ------------------------------------------------------------ sua file .env

def read_env_lines(path: Path) -> list[str]:
    """Doc .env thanh list dong (giu nguyen ky tu, khong newline cuoi moi phan tu).

    Chua co .env -> seed tu .env.example de nguoi dung giu duoc cac comment
    huong dan; khong co ca hai -> bat dau tu file rong.
    """
    if path.exists():
        return path.read_text(encoding="utf-8").splitlines()
    if ENV_EXAMPLE_PATH.exists() and path == ENV_PATH:
        return ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    return []


def get_env_value(lines: list[str], key: str) -> str | None:
    """Gia tri hien tai cua `key` trong cac dong .env; khong co dong do -> None."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped[len(key) + 1 :].strip()
    return None


def set_env_line(lines: list[str], key: str, value: str) -> list[str]:
    """Sua DUNG dong `KEY=` (giu nguyen comment + moi dong khac); chua co thi append."""
    out = list(lines)
    for i, line in enumerate(out):
        if line.strip().startswith(f"{key}="):
            out[i] = f"{key}={value}"
            return out
    if out and out[-1].strip():
        out.append("")  # cach 1 dong cho de doc
    out.append(f"{key}={value}")
    return out


def _write_file_0600(path: Path, data: str) -> None:
    """Mo file voi mode 600 NGAY TU LUC TAO (khong co khoang ho umask), roi ghi."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    # file ton tai tu truoc thi O_CREAT khong doi quyen -> ep lai 600
    os.chmod(path, 0o600)


def write_env(path: Path, lines: list[str], *, backup: bool = True) -> None:
    """Ghi .env: backup `.env.bak` truoc (neu file cu ton tai), ca hai deu 600 tu luc tao."""
    if backup and path.exists():
        bak = path.with_name(path.name + ".bak")
        _write_file_0600(bak, path.read_text(encoding="utf-8"))
    _write_file_0600(path, "\n".join(lines) + "\n")


# ------------------------------------------------------------ validate key

def validate_key(
    preset: ProviderPreset, api_key: str | None, model: str | None = None
) -> tuple[int, str]:
    """Goi 1 request nho toi provider de kiem tra key song.

    Tra (latency_ms, model_tra_loi); loi (key sai, het quota, khong co mang...)
    thi de exception noi len cho caller bao loi. Khong boc budget — day la
    request kiem tra, ngoai vong doi task.
    """
    from laplace.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(
        api_key=api_key or "ollama",
        model=model or preset.default_model,
        base_url=preset.base_url,
        name=preset.name,
        pricing=preset.pricing,
        warn_unknown_price=False,
    )
    start = time.monotonic()
    result = provider.complete(
        [{"role": "user", "content": "Tra loi dung 1 tu: OK"}]
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    return latency_ms, result.model or preset.default_model


# ------------------------------------------------------------ wizard

def _choose_preset() -> ProviderPreset | None:
    presets = list(PRESETS.values())
    print("Chon hang AI de noi API key:\n")
    for i, p in enumerate(presets, start=1):
        need = f"key: {p.key_url}" if p.requires_key else f"khong can key — cai: {p.key_url}"
        print(f"  {i}. {p.name:<10} — {p.label}")
        print(f"     {need} | free tier: {p.free_tier}")
    print()
    raw = input(f"Nhap so (1-{len(presets)}, Enter de thoat): ").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        idx = 0
    if not 1 <= idx <= len(presets):
        print("Lua chon khong hop le.")
        return None
    return presets[idx - 1]


def main() -> int:
    preset = _choose_preset()
    if preset is None:
        return 1

    api_key: str | None = None
    if preset.requires_key:
        # getpass: khong echo ra man hinh, khong vao shell history
        api_key = getpass.getpass(
            f"Dan API key cua {preset.label} (khong hien thi khi go): "
        ).strip()
        if not api_key:
            print("Khong nhan duoc key — thoat, chua ghi gi vao .env.")
            return 1

    target = preset.base_url or "https://api.openai.com/v1"
    print(f"Dang goi thu 1 request nho toi {target} ...")
    try:
        latency_ms, model = validate_key(preset, api_key)
    except Exception as e:
        print(f"Key KHONG dung duoc: {type(e).__name__}: {e}")
        print(f"Kiem tra lai key tai: {preset.key_url} — chua ghi gi vao .env.")
        return 1
    print(f"Key song! Model '{model}' tra loi sau {latency_ms} ms.")

    lines = read_env_lines(ENV_PATH)
    if preset.env_key:
        existing = get_env_value(lines, preset.env_key)
        if existing and existing != api_key:
            answer = input(
                f"{preset.env_key} da co gia tri ({mask_key(existing)}). Ghi de? [y/N]: "
            ).strip().lower()
            if answer not in ("y", "yes"):
                print("Giu key cu — thoat, khong thay doi .env.")
                return 1
        assert api_key is not None
        lines = set_env_line(lines, preset.env_key, api_key)
    lines = set_env_line(lines, "LAPLACE_LLM_PROVIDER", preset.name)

    write_env(ENV_PATH, lines, backup=True)
    shown = mask_key(api_key) if preset.requires_key else "(khong can key)"
    print(f"Da ghi {ENV_PATH} (backup: .env.bak, chmod 600).")
    print(f"  LAPLACE_LLM_PROVIDER={preset.name}")
    if preset.env_key:
        print(f"  {preset.env_key}={shown}")
    print("Kiem tra lai bat cu luc nao: python -m laplace.llm.check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
