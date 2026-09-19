from pathlib import Path
import csv

SRC_DIRS = [
    Path("results/drtp/k8s_same_scale/tables_norm_final"),
    Path("results/drtp/k8s_same_scale/tables_delay_frag"),
]

DST_SUFFIX = "_no_fgor"
TARGET = "FG-orig"

def split_md_row(line):
    return [x.strip() for x in line.strip().strip("|").split("|")]

def join_md_row(cells):
    return "| " + " | ".join(cells) + " |"

def process_md_text(text):
    out = []
    remove_col_idx = None
    inside_table = False

    for line in text.splitlines():
        stripped = line.strip()

        # 非表格行
        if not stripped.startswith("|"):
            inside_table = False
            remove_col_idx = None
            out.append(line)
            continue

        cells = split_md_row(line)

        # 整行包含 FG-orig，且它是 method 行，例如 | FG-orig | ...
        # 或 detail 表里第二列是 FG-orig，例如 | 200 | FG-orig | ...
        if any(c == TARGET for c in cells):
            # header 里有 FG-orig：删列
            if TARGET in cells:
                idx = cells.index(TARGET)
                # 如果这一行是矩阵表头，比如 | requests | ... | FG-orig | FG-selected |
                if not all(set(c) <= set("-: ") for c in cells):
                    remove_col_idx = idx
                    inside_table = True
                    new_cells = [c for i, c in enumerate(cells) if i != remove_col_idx]
                    out.append(join_md_row(new_cells))
                    continue

            # 如果是数据行，且某个 cell 恰好是 FG-orig，整行删除
            continue

        # 表格分隔行
        if cells and all(set(c) <= set("-: ") for c in cells):
            if remove_col_idx is not None and remove_col_idx < len(cells):
                cells = [c for i, c in enumerate(cells) if i != remove_col_idx]
            out.append(join_md_row(cells))
            continue

        # 普通表格行：如果当前表头删除过 FG-orig 列，则同步删对应列
        if remove_col_idx is not None and remove_col_idx < len(cells):
            cells = [c for i, c in enumerate(cells) if i != remove_col_idx]
            out.append(join_md_row(cells))
        else:
            out.append(line)

    return "\n".join(out) + "\n"

def process_csv(src, dst):
    with open(src, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        dst.write_text("", encoding="utf-8")
        return

    header = rows[0]
    remove_idx = [i for i, h in enumerate(header) if h == TARGET]

    new_rows = []
    for row in rows:
        # 删除 method == FG-orig 的行
        if any(cell == TARGET for cell in row):
            continue

        # 删除 FG-orig 列
        new_row = [cell for i, cell in enumerate(row) if i not in remove_idx]
        new_rows.append(new_row)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(new_rows)

def process_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.suffix == ".md":
        text = src.read_text(encoding="utf-8")
        dst.write_text(process_md_text(text), encoding="utf-8")
    elif src.suffix == ".csv":
        process_csv(src, dst)
    else:
        dst.write_bytes(src.read_bytes())

for src_dir in SRC_DIRS:
    if not src_dir.exists():
        print("[SKIP missing]", src_dir)
        continue

    dst_dir = Path(str(src_dir) + DST_SUFFIX)
    dst_dir.mkdir(parents=True, exist_ok=True)

    for src in src_dir.rglob("*"):
        if src.is_file():
            rel = src.relative_to(src_dir)
            dst = dst_dir / rel
            process_file(src, dst)

    print("[DONE]", src_dir, "->", dst_dir)
