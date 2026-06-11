# -*- coding: utf-8 -*-
"""v3 批量处理脚本 - 直接内联版"""
import json
import sys
import time
from pathlib import Path
from pdf_txt_report_splitter_v2 import TxtReportSplitter

INPUT_DIR = Path("full")
OUTPUT_DIR = Path("output/full_split_v3")
BATCH_SIZE = 50

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(INPUT_DIR.glob("*.txt"))
    total = len(files)

    sys.stdout.write(f"v3 start: {total} files\n")
    sys.stdout.flush()

    progress_path = OUTPUT_DIR / "_progress.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        processed_ids = set(progress.get("processed", []))
        details = progress.get("details", [])
        sys.stdout.write(f"resume: {len(processed_ids)}\n")
        sys.stdout.flush()
    else:
        processed_ids = set()
        details = []

    splitter = TxtReportSplitter()
    start_time = time.time()

    for idx, fp in enumerate(files, 1):
        doc_id = fp.stem
        if doc_id in processed_ids:
            continue

        try:
            result = splitter.split_file(fp, OUTPUT_DIR, structured=True)
        except Exception as e:
            detail = {"doc_id": doc_id, "status": "failed", "error": str(e)}
            details.append(detail)
            continue

        detail = {
            "doc_id": doc_id,
            "status": result.status,
            "sections": list(result.sections.keys()),
            "section_sizes": {k: len(v) for k, v in result.sections.items()},
        }
        details.append(detail)
        processed_ids.add(doc_id)

        if idx % 50 == 0 or idx == total:
            elapsed = time.time() - start_time
            speed = len(processed_ids) / elapsed if elapsed > 0 else 0
            sys.stdout.write(f"[{idx}/{total}] speed={speed:.1f}f/s processed={len(processed_ids)}\n")
            sys.stdout.flush()

        if len(processed_ids) % BATCH_SIZE == 0:
            progress_path.write_text(
                json.dumps({"processed": list(processed_ids), "details": details}, ensure_ascii=False),
                encoding="utf-8"
            )

    summary = {
        "total_files": total,
        "success": sum(1 for d in details if d["status"] == "success"),
        "warning": sum(1 for d in details if d["status"] == "warning"),
        "failed": sum(1 for d in details if d["status"] == "failed"),
        "ignored_summary": sum(1 for d in details if d["status"] == "ignored_summary"),
        "ignored_low_quality": sum(1 for d in details if d["status"] == "ignored_low_quality"),
        "elapsed_seconds": round(time.time() - start_time, 2),
        "details": details,
    }
    (OUTPUT_DIR / "_batch_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sys.stdout.write(f"DONE: success={summary['success']} elapsed={summary['elapsed_seconds']}s\n")
    sys.stdout.flush()

    progress_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
