from pathlib import Path
import re
import sys

src = Path("scripts/fg_dscr.py")
dst = Path("scripts/fg_dscr_online_norm.py")

s = src.read_text(encoding="utf-8")
orig = s

helper = r'''
# =========================
# [ONLINE-NORM PATCH]
# Online min-max normalization for runtime decision making.
# Benefit terms: larger is better.
# Cost terms: smaller is better, converted to saving score.
# =========================
def _fg_online_norm_rows(rows, benefit_keys=(), cost_keys=(), eps=1e-12):
    """
    rows: list[dict]
    For each key, normalize within the current candidate set.
    If all candidates have the same value, assign neutral score 0.5.
    """
    if not rows:
        return rows

    for key in benefit_keys:
        vals = [float(r.get(key, 0.0)) for r in rows]
        lo, hi = min(vals), max(vals)
        for r in rows:
            x = float(r.get(key, 0.0))
            if abs(hi - lo) < eps:
                r[key + "_norm"] = 0.5
            else:
                r[key + "_norm"] = (x - lo) / (hi - lo)

    for key in cost_keys:
        vals = [float(r.get(key, 0.0)) for r in rows]
        lo, hi = min(vals), max(vals)
        for r in rows:
            x = float(r.get(key, 0.0))
            if abs(hi - lo) < eps:
                r[key + "_save_norm"] = 0.5
            else:
                r[key + "_save_norm"] = (hi - x) / (hi - lo)

    return rows


def _fg_get_float(obj, keys, default=0.0):
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            try:
                return float(obj[k])
            except Exception:
                return default
    return default
'''

if "_fg_online_norm_rows" not in s:
    # 插到 import 区域之后
    m = re.search(r"((?:^import .*\n|^from .*\n|\n)+)", s, flags=re.M)
    if m:
        pos = m.end()
        s = s[:pos] + "\n" + helper + "\n" + s[pos:]
    else:
        s = helper + "\n" + s

# ------------------------------------------------------------
# 1) 添加 CLI 参数：--online-norm
# ------------------------------------------------------------
if "--online-norm" not in s:
    # 找 argparse parser.add_argument 区域，在 --algo-name 后面或 parse_args 前插入
    insert_cli = '''
    parser.add_argument("--online-norm", action="store_true",
                        help="Enable online component-wise normalization during phase-1 placement and phase-2 ordering decisions.")
'''
    # 优先插到 algo-name 后
    pat = r'(parser\.add_argument\([^\n]*"--algo-name"[\s\S]*?\)\n)'
    m = re.search(pat, s)
    if m:
        s = s[:m.end()] + insert_cli + s[m.end():]
    else:
        m = re.search(r'(\s*args\s*=\s*parser\.parse_args\(\)\n)', s)
        if not m:
            print("[ERROR] Cannot find argparse insertion point.", file=sys.stderr)
            dst.write_text(s, encoding="utf-8")
            sys.exit(2)
        s = s[:m.start()] + insert_cli + s[m.start():]

# ------------------------------------------------------------
# 2) 把 args.online_norm 传给调度器对象 / 配置对象
# 尽量以低侵入方式：在 args 传入后，如果对象有属性则 setattr
# ------------------------------------------------------------
if "online_norm=bool(getattr(args, \"online_norm\", False))" not in s:
    # 尝试在构造 FG 类时添加 online_norm 参数
    # 更稳的方式：在 main 里 parse_args 后加入全局标记
    marker = 'args = parser.parse_args()'
    if marker in s and "_FG_ONLINE_NORM_ENABLED" not in s:
        s = s.replace(
            marker,
            marker + '\n    globals()["_FG_ONLINE_NORM_ENABLED"] = bool(getattr(args, "online_norm", False))',
            1
        )

# ------------------------------------------------------------
# 3) patch phase-2 dynamic gain：寻找包含 reuse/future/pull/evict 的 gain 计算块
#    如果原代码已有 gain = a*reuse + ... - ...，替换成在线归一化候选选择。
#    由于不同版本函数名可能不同，这里先插入一个通用函数，然后替换典型的 max loop。
# ------------------------------------------------------------
phase2_helper = r'''
def _fg_select_by_online_norm_dynamic_gain(candidate_rows, alpha_reuse=1.0, alpha_future=1.0, alpha_pull=1.0, alpha_evict=1.0):
    """
    candidate_rows requires:
      cid, reuse, future, pull, evict
    returns best row after online normalization.
    """
    _fg_online_norm_rows(
        candidate_rows,
        benefit_keys=("reuse", "future"),
        cost_keys=("pull", "evict"),
    )
    for r in candidate_rows:
        denom = alpha_reuse + alpha_future + alpha_pull + alpha_evict
        if abs(denom) < 1e-12:
            denom = 1.0
        r["gain_norm"] = (
            alpha_reuse  * r.get("reuse_norm", 0.5)
            + alpha_future * r.get("future_norm", 0.5)
            + alpha_pull   * r.get("pull_save_norm", 0.5)
            + alpha_evict  * r.get("evict_save_norm", 0.5)
        ) / denom
    return max(candidate_rows, key=lambda x: (x.get("gain_norm", -1e18), x.get("reuse_norm", 0.0), -x.get("pull", 0.0)))
'''

if "_fg_select_by_online_norm_dynamic_gain" not in s:
    # 插在 helper 后
    s = s.replace(helper, helper + "\n" + phase2_helper + "\n", 1)

# ------------------------------------------------------------
# 4) patch phase-1 potential：通用函数
# ------------------------------------------------------------
phase1_helper = r'''
def _fg_select_node_by_online_norm_potential(candidate_rows, lambda_delay=1.0, lambda_frag=0.1, lambda_aff=0.2, lambda_load=0.2):
    """
    candidate_rows requires:
      node/node_id, delay, frag, aff, load
    Lower normalized potential is better.
    delay/frag/load are cost terms; aff is benefit term.
    """
    _fg_online_norm_rows(
        candidate_rows,
        benefit_keys=("aff",),
        cost_keys=("delay", "frag", "load"),
    )
    for r in candidate_rows:
        # save_norm: larger is better. Convert to potential by using 1-save_norm for cost.
        delay_cost = 1.0 - r.get("delay_save_norm", 0.5)
        frag_cost  = 1.0 - r.get("frag_save_norm", 0.5)
        load_cost  = 1.0 - r.get("load_save_norm", 0.5)
        aff_reward = r.get("aff_norm", 0.5)
        r["phi_norm"] = (
            lambda_delay * delay_cost
            + lambda_frag * frag_cost
            - lambda_aff * aff_reward
            + lambda_load * load_cost
        )
    return min(candidate_rows, key=lambda x: (x.get("phi_norm", 1e18), x.get("delay", 1e18)))
'''

if "_fg_select_node_by_online_norm_potential" not in s:
    s = s.replace(phase2_helper, phase2_helper + "\n" + phase1_helper + "\n", 1)

# ------------------------------------------------------------
# 5) 尝试自动替换二阶段候选 gain 选择逻辑
#    这里采用“追加在线归一化候选选择分支”的策略：
#    找到类似 best_u/best_gain/max_gain 的 loop，如果失败就提示人工定位。
# ------------------------------------------------------------

patched_phase2 = False

# Pattern A: candidate list with gain dict already exists
# 替换常见形式：best = max(candidates, key=lambda x: x["gain"])
patterns_phase2 = [
    (
        r'best\s*=\s*max\((?P<lst>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*key\s*=\s*lambda\s+\w+\s*:\s*\w+\[[\'"]gain[\'"]\]\s*\)',
        'best = (_fg_select_by_online_norm_dynamic_gain(\\g<lst>) if globals().get("_FG_ONLINE_NORM_ENABLED", False) else max(\\g<lst>, key=lambda x: x["gain"]))'
    ),
    (
        r'best_row\s*=\s*max\((?P<lst>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*key\s*=\s*lambda\s+\w+\s*:\s*\w+\.get\([\'"]gain[\'"],\s*[-0-9.eE]+\)\s*\)',
        'best_row = (_fg_select_by_online_norm_dynamic_gain(\\g<lst>) if globals().get("_FG_ONLINE_NORM_ENABLED", False) else max(\\g<lst>, key=lambda x: x.get("gain", -1e18)))'
    ),
]

for pat, rep in patterns_phase2:
    ns, n = re.subn(pat, rep, s, count=20)
    if n > 0:
        s = ns
        patched_phase2 = True

# Pattern B: 如果存在 phase2_reuse_history 说明计算过四分量，但没有 candidates max 形式
# 这里只标记，后面会输出提醒
has_phase2_terms = all(x in s.lower() for x in ["reuse", "future", "pull", "evict"])

# ------------------------------------------------------------
# 6) 尝试自动替换一阶段候选节点选择逻辑
# ------------------------------------------------------------
patched_phase1 = False
patterns_phase1 = [
    (
        r'best\s*=\s*min\((?P<lst>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*key\s*=\s*lambda\s+\w+\s*:\s*\w+\[[\'"]phi[\'"]\]\s*\)',
        'best = (_fg_select_node_by_online_norm_potential(\\g<lst>) if globals().get("_FG_ONLINE_NORM_ENABLED", False) else min(\\g<lst>, key=lambda x: x["phi"]))'
    ),
    (
        r'best_node\s*=\s*min\((?P<lst>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*key\s*=\s*lambda\s+\w+\s*:\s*\w+\.get\([\'"]phi[\'"],\s*[0-9.eE]+\)\s*\)',
        'best_node = (_fg_select_node_by_online_norm_potential(\\g<lst>) if globals().get("_FG_ONLINE_NORM_ENABLED", False) else min(\\g<lst>, key=lambda x: x.get("phi", 1e18)))'
    ),
    (
        r'best_node\s*=\s*min\((?P<lst>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*key\s*=\s*lambda\s+\w+\s*:\s*\w+\[[\'"]score[\'"]\]\s*\)',
        'best_node = (_fg_select_node_by_online_norm_potential(\\g<lst>) if globals().get("_FG_ONLINE_NORM_ENABLED", False) else min(\\g<lst>, key=lambda x: x["score"]))'
    ),
]

for pat, rep in patterns_phase1:
    ns, n = re.subn(pat, rep, s, count=20)
    if n > 0:
        s = ns
        patched_phase1 = True

# ------------------------------------------------------------
# 7) 如果没能自动 patch 决策点，仍输出脚本，但告诉用户精确下一步 grep。
# ------------------------------------------------------------
dst.write_text(s, encoding="utf-8")

print("[OK] wrote", dst)
print("[PATCH STATUS] phase1_decision_patched =", patched_phase1)
print("[PATCH STATUS] phase2_decision_patched =", patched_phase2)
print("[INFO] phase2_terms_exist =", has_phase2_terms)

if not patched_phase1 or not patched_phase2:
    print()
    print("[WARN] 自动补丁没有完全定位到一阶段或二阶段的最终选择语句。")
    print("[WARN] 已经生成 scripts/fg_dscr_online_norm.py，但需要进一步确认是否真正接入决策。")
    print("[WARN] 请看下面定位输出：")
    print()
    for pat in ["best", "best_node", "gain", "phi", "potential", "candidate"]:
        print(f"===== grep {pat} =====")
        for i, line in enumerate(s.splitlines(), 1):
            if pat in line:
                if any(k in line for k in ["best", "gain", "phi", "potential", "candidate", "score"]):
                    print(f"{i}: {line[:220]}")
    sys.exit(3)
