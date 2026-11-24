#!/usr/bin/env python3

import os
import re
import shutil
import subprocess
import tempfile


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LLMGEN_RS_PATH = os.path.join(ROOT_DIR, "tquic", "src", "congestion_control", "llmgen.rs")


def write_text(path, content):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def extract_rust_code(content):
    matches = re.findall(r"```(?:rust|rs)?\s*(.*?)\s*```", content or "", re.DOTALL | re.IGNORECASE)
    if matches:
        return matches[-1].strip()
    return ""


def strip_code_blocks(content):
    return re.sub(r"```(?:rust|rs)?\s*.*?\s*```", "", content or "", flags=re.DOTALL | re.IGNORECASE).strip()


def trim_compiler_feedback(output, max_chars=12000):
    text = (output or "").strip()
    if not text:
        return "cargo check failed with no compiler output."
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def compile_check_candidate(code_path, compile_log_path):
    os.makedirs(os.path.dirname(compile_log_path), exist_ok=True)

    fd, backup_path = tempfile.mkstemp(prefix="llmgen.rs.backup.", suffix=".tmp")
    os.close(fd)
    shutil.copyfile(LLMGEN_RS_PATH, backup_path)
    try:
        shutil.copyfile(code_path, LLMGEN_RS_PATH)
        proc = subprocess.run(
            ["cargo", "check", "--manifest-path", os.path.join(ROOT_DIR, "tquic", "Cargo.toml"), "--workspace"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        write_text(compile_log_path, combined)
        return proc.returncode == 0, combined
    finally:
        shutil.copyfile(backup_path, LLMGEN_RS_PATH)
        try:
            os.remove(backup_path)
        except OSError:
            pass
