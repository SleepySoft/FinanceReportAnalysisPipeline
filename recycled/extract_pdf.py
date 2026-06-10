#!/usr/bin/env python3
"""用 pdfplumber 重新提取 PDF 文本，对比质量"""
import pdfplumber
import sys

pdf_path = sys.argv[1]
out_path = sys.argv[2]

with pdfplumber.open(pdf_path) as pdf:
    text = ""
    for i, page in enumerate(pdf.pages):
        page_text = page.extract_text()
        if page_text:
            text += f"\n--- Page {i+1} ---\n"
            text += page_text
            text += "\n"

with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)

print(f"提取完成: {len(text)} 字符")
