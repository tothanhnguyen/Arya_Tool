"""Kiem tra API key: `python -m laplace.llm.check` (T15).

Doc .env hien tai, in provider/model dang chon, goi thu 1 request nho,
bao key song/chet + latency. `--all` thu MOI hang da co key trong .env
(ollama khong can key nen luon duoc thu — chet nhanh neu ollama chua chay).

Key chi hien thi dang mask (6 ky tu dau + "...").
"""

import argparse
import sys

from laplace.llm.base import resolve_provider_config
from laplace.llm.presets import PRESETS, ProviderPreset, mask_key
from laplace.llm.setup import validate_key


def check_preset(preset: ProviderPreset) -> bool:
    """Thu 1 request voi key/model tu settings; in ket qua, tra True neu song."""
    _, api_key, model = resolve_provider_config(preset.name)
    key_shown = mask_key(api_key) if preset.requires_key else "(khong can key)"
    print(f"[{preset.name}] model={model} key={key_shown}")
    try:
        latency_ms, replied_model = validate_key(preset, api_key, model)
    except Exception as e:
        print(f"  -> CHET: {type(e).__name__}: {e}")
        print(f"  -> Lay/kiem tra key tai: {preset.key_url}")
        return False
    print(f"  -> SONG: model '{replied_model}' tra loi sau {latency_ms} ms")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m laplace.llm.check",
        description="Kiem tra API key LLM song/chet (goi 1 request nho).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="thu moi hang da co key trong .env (mac dinh: chi hang dang chon)",
    )
    args = parser.parse_args(argv)

    from laplace.config import get_settings

    settings = get_settings()

    if args.all:
        ok = True
        tried = 0
        for preset in PRESETS.values():
            _, api_key, _ = resolve_provider_config(preset.name)
            if preset.requires_key and not api_key:
                print(f"[{preset.name}] bo qua — chua co key ({preset.env_key})")
                continue
            tried += 1
            ok = check_preset(preset) and ok
        if tried == 0:
            print("Chua co key nao trong .env — chay: python -m laplace.llm.setup")
            return 1
        return 0 if ok else 1

    name = settings.llm_provider
    print(f"Provider dang chon (LAPLACE_LLM_PROVIDER): {name}")
    if name not in PRESETS:
        print("  -> mock/khong ro: chay offline, khong can key. Khong co gi de kiem tra.")
        return 0
    preset = PRESETS[name]
    _, api_key, _ = resolve_provider_config(name)
    if preset.requires_key and not api_key:
        print(f"  -> CHUA CO KEY: dat {preset.env_key} trong .env")
        print(f"  -> Lay key tai: {preset.key_url} (hoac: python -m laplace.llm.setup)")
        return 1
    return 0 if check_preset(preset) else 1


if __name__ == "__main__":
    sys.exit(main())
