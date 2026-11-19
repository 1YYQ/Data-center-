"""Trusted fixed-version SDK entry; executed only by the isolated MinerU Python.

The SDK writes into the persistent attempt directory, unlike mineru-kit's ZIP
temporary directory. Even failed saves leave the actual partial outputs here.
"""
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> int:
    from mineru.version import __version__
    if __version__ != "4.2.3":
        print("PARSE_PROFILE_MISMATCH", file=sys.stderr)
        return 2
    from mineru import parse
    from mineru.config import VlmConfig, get_config
    from mineru.parser.writer import FileBasedDataWriter

    source, workspace = Path(sys.argv[1]), Path(sys.argv[2])
    output = workspace / "output"
    output.mkdir(exist_ok=False)
    config = get_config()
    if (config.model.vlm.server_url or config.llm_aided.features.title_leveling
            or config.llm_aided.features.cross_page_table_cell_merge):
        print("LOCAL_PARSE_CONFIGURATION_DENIED", file=sys.stderr)
        return 2
    result = parse(source, tier="standard", ocr_mode="auto", image_analysis=True, page_range="",
                   vlm_config=VlmConfig(engine="llama-cpp", server_url="", api_key="", model=""))
    # Persist a raw representation even if the resource/renderer save fails.
    with (workspace / "raw_middle.json").open("x", encoding="utf-8") as raw:
        raw.write(result.to_json())
        raw.flush()
        import os
        os.fsync(raw.fileno())
    result.save(FileBasedDataWriter(str(output)))
    with ZipFile(workspace / "result.zip", "x", ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(output).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
