"""CMS 片头印制广告「自帧处理」：检测器 + fill/blur 管线（2026-11-04）。

主路线（站主 2026-11-04 亲定）：自帧处理为主、借帧降为精修。
本模块给 #1818 的换段下发机器换一种修复来源——不用别人家的
画面，只处理这部片自己的片头带窗分段：带区背景均匀/简单 →
填充真去掉；复杂画面 → 强模糊到赞助字不可辨读。产物分段、
记账表（cms_graft_repairs，method=self_fill/self_blur）、换段
下发、断点续跑全部与借帧线共用（见 cms_graft）；本模块只负责
「检测 + 自帧修复 + 修后复检」这一段。

检测器定版（试点 self-frame-pilot-20261104 帧图亲验后重做，
第一版作废的教训在案）：

- 整窗拼接解码（禁按秒抽帧）→ 2fps 低清灰度 → 5 秒块中位帧；
- 条带几何：逐行横向梯度剖面，显著行段按间隙 ≤6 行合并后取
  最重组、上下各扩 1 行（旧版只圈最强一行、第二行整行残留）；
- 带尾：逐秒 2D 梯度匹配滤波——只认同位置同字形的字边（1D 行
  剖面分不清赞助字与同带演职员字幕，曾把带尾高估到 54s）；
- 标注 hint（摸底 strip）只作先验：检测在全区（top/mid/bottom）
  各扫一遍候选，结论以持续性更强者为准，冲突记 conflict。

三条门控（试点三类事故各对应一条，fail-closed）：

1. 跨场景持续性：候选带须在 ≥2 个场景互异的 5 秒块里同位置同
   字形命中——片头提示文字/声明只活在自己那张卡里，横跨卡片
   与正片场景的才可能是赞助带（挡「提示文字当带填成黑条」）。
2. hint 只作先验、全区扫描（见上；挡摸底 strip 标注错位时只在
   错区里找、把片名图形当带处理）。
3. 字幕行隔离：条带下沿吃进画面底部 8% 字幕区时，逐行按持续
   性拆分赞助行与字幕行，只处理赞助行；拆不开 → 整条放弃
   （挡条带过合并把对白字幕行一并抹掉）。

修后复检：修完用同一套 2D 梯度口径重跑——带窗内逐秒比值须
全部 <0.30（带还在即判失败、不下发）且带外 MAD 不超阈（带外
损伤超阈同样判失败）；带尾一律外扩 2 秒兜底（试点实证带尾
低估约 2 秒），但修复窗不许越过口子。

时间中值与同剧片头互借本轮不进生产路径：试点已证本批带字
全静态（滚动 0/8，中值无用武之地）、同源同季同模板逐集烧录
无供体（互借 0/21 格）——判定代码思路留档于试点报告
self-frame-pilot-20261104.md，不在此实现，防误启。

熔断沿用试点收紧版（比借帧线 host_ok 更严）：可用内存
<1.2GB / load1 >6 / health TTFB >1s 连续两次，任一命中不开工。
工人入口仍在 cms_graft_worker（--method self），并发/家宽
信号量/记账复用借帧线机制。
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

WORKER_VERSION = "1.0"

#: 检测低清宽（口径同试点 DET_W 与 graft 低清分析）。
DET_W = 480
#: 逐秒/逐块 2D 梯度匹配的「带在场」阈值（试点标定）。
RATIO_THR = 0.30
#: 带尾外扩保险（秒）：试点实证带尾低估约 2 秒。
TAIL_EXPAND_SEC = 2.0
#: 字幕区：画面底部占比（条带吃进此区触发字幕行隔离门控）。
SUBTITLE_ZONE_FRAC = 0.08
#: 跨场景判定：两块带外 MAD ≥ 此值视为不同场景（0–255 标度）。
SCENE_DIFF_MIN = 4.0
#: 修后复检带外损伤上限（带外灰度 MAD 均值；试点成功条 ≤1.21，
#: 留余量取 3.0，检错带事故条为 4.0–7.0）。
POST_OUT_MAD_MAX = 3.0

#: 全区扫描的三个候选区（ det 坐标系纵向占比，试点 ZONE）。
ZONES = {"top": (0.00, 0.30), "mid": (0.30, 0.72),
         "bottom": (0.66, 1.00)}

_UNSET = "\x00cms-self-frame-unset\x00"


# ---------------------------------------------------------------- 熔断（试点收紧版）


def host_ok_strict(mem_available_kb, load1) -> bool:
    """自帧线熔断谓词：可用内存 ≥1.2GB 且 load1 ≤6 才开工。"""
    try:
        return (float(mem_available_kb) >= 1_200_000
                and float(load1) <= 6.0)
    except (TypeError, ValueError):
        return False


def health_ttfb(url="http://127.0.0.1:8000/api/health",
                timeout=8) -> float:
    """本机 health 端到端耗时（秒）；失败按 99（必熔断）。"""
    try:
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read()
        return time.time() - t0
    except Exception:  # noqa: BLE001 — 探不到健康口径就不许开工
        return 99.0


def host_gate_ok(check_ttfb=True) -> bool:
    """三线合一的即时判定（TTFB 连续两次超 1s 的计数由等待环维护，
    这里只做单次探查：TTFB 超限直接判不过，宁严勿扰主链路）。"""
    from backend.emby_server import cms_graft as graft

    mem_kb, load1 = graft.host_state()
    if not host_ok_strict(mem_kb, load1):
        return False
    if check_ttfb and health_ttfb() > 1.0:
        return False
    return True


# ---------------------------------------------------------------- 图像原语（试点定版）


def median_image(imgs):
    """一组同尺寸灰度帧的逐像素中位图（静态烧录字在中位帧最清楚）。"""
    if len(imgs) == 1:
        return imgs[0].copy()
    datas = [list(im.getdata()) for im in imgs]
    med = [statistics.median(px) for px in zip(*datas)]
    out = imgs[0].copy()
    out.putdata([int(v) for v in med])
    return out


def row_grad(img):
    """逐行横向梯度均值剖面（prober 同口径）。"""
    from PIL import Image, ImageChops

    w, h = img.size
    core = img.crop((0, 0, w - 1, h))
    shifted = img.crop((1, 0, w, h))
    d = ImageChops.difference(core, shifted)
    return list(d.resize((1, h), Image.BOX).getdata())


def best_run(profile, lo, hi, thr):
    """[lo,hi) 内 profile≥thr 的行段：间隙 ≤6 行先合并、再取最重组。

    返回 (y0, y1, total) 或 None。合并是定版关键：赞助卡常是两行
    字，只取最强一行会整行残留（试点第一版实证）。
    """
    runs = []
    i = lo
    while i < hi:
        if profile[i] < thr:
            i += 1
            continue
        j = i
        tot = 0.0
        while j < hi and profile[j] >= thr:
            tot += profile[j]
            j += 1
        runs.append([i, j, tot])
        i = j
    if not runs:
        return None
    merged = [runs[0]]
    for r in runs[1:]:
        if r[0] - merged[-1][1] <= 6:
            merged[-1][1] = r[1]
            merged[-1][2] += r[2]
        else:
            merged.append(r)
    best = max(merged, key=lambda x: x[2])
    return (best[0], best[1], best[2])


def grad_strip(img, y0, y1):
    """条带区的 2D 梯度幅值列表（dx+dy，逐像素）。

    只认「同位置同形状的字边」：半透明叠印只改亮度不改边位置，
    仍命中；演职员字幕位置字形都不同，比值自然低。
    """
    from PIL import ImageChops

    w, h = img.size
    dx = ImageChops.difference(img.crop((0, 0, w - 1, h)),
                               img.crop((1, 0, w, h)))
    dy = ImageChops.difference(img.crop((0, 0, w, h - 1)),
                               img.crop((0, 1, w, h)))
    dxd = list(dx.getdata())
    dyd = list(dy.getdata())
    out = []
    for y in range(y0, y1):
        row_dx = dxd[y * (w - 1):(y + 1) * (w - 1)]
        yy = min(y, h - 2)
        row_dy = dyd[yy * w:(yy + 1) * w]
        out.extend([a + b for a, b in zip(row_dx, row_dy[:w - 1])])
    return out


def grad_ratio(template, grad) -> float:
    """逐像素 min 匹配比：grad 与 template 同位置同字形的重合度。"""
    denom = sum(template) or 1.0
    return sum(min(a, b) for a, b in zip(grad, template)) / denom


def outside_mad(img_a, img_b, strip) -> float:
    """两帧在条带之外的灰度平均绝对差（场景异同/带外损伤口径）。"""
    from PIL import ImageChops, ImageStat

    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size)
    diff = ImageChops.difference(
        img_a.convert("L"), img_b.convert("L"))
    w, h = diff.size
    y0, y1 = int(strip[0]), int(strip[1])
    total = 0.0
    area = 0
    for box in ((0, 0, w, max(0, y0)), (0, min(h, y1), w, h)):
        if box[3] <= box[1]:
            continue
        region = diff.crop(box)
        total += ImageStat.Stat(region).mean[0] * region.size[0] \
            * region.size[1]
        area += region.size[0] * region.size[1]
    return total / area if area else 0.0


# ---------------------------------------------------------------- 检测（纯判定层）


def block_medians(frames):
    """2fps 帧列 → 5 秒块中位帧（每块 10 帧，尾块不足丢弃）。"""
    return [median_image(frames[b * 10:(b + 1) * 10])
            for b in range(len(frames) // 10)]


def second_medians(frames):
    """2fps 帧列 → 逐秒中位帧（每秒 2 帧）。"""
    return [median_image(frames[s * 2:(s + 1) * 2])
            for s in range(len(frames) // 2)]


def _zone_candidate(blocks, zlo, zhi):
    """一个候选区内的最强块条带：返回 (peak_block, run, score)。"""
    best = None
    for b, med in enumerate(blocks):
        prof = row_grad(med)
        base = statistics.median(prof)
        thr = max(5.0, base * 2.0)
        run = best_run(prof, zlo, zhi, thr)
        if run is None:
            continue
        if best is None or run[2] > best[2]:
            best = (b, run, run[2])
    return best


#: 场景切变证据：相邻秒带外 MAD ≥ 此值视为带外内容换了一轮
#: （黑卡赞助带实证：出品卡 20 秒不动、卡内演职员字行换了两
#: 次，切变 5.5——块间 MAD 被 5 秒中位冲淡到 ≤3，此口径才抓
#: 得住；单卡静态画面相邻秒 MAD ≈0）。
SCENE_CUT_MIN = 5.0
#: 切变路径附加条件：切变两侧逐秒比值都 ≥ 此值（带须全强度
#: 在场，不许拿半强度边缘命中凑切变证据）
SCENE_CUT_RATIO_MIN = 0.6


def _persistence(blocks, sec_meds, peak_b, strip_det, template):
    """跨场景持续性：命中块数 + 场景互异证据。

    命中 = 该块中位帧条带与模板的 2D 梯度比 ≥RATIO_THR。
    场景互异（任一成立）：①两命中块的带外 MAD ≥SCENE_DIFF_MIN；
    ②带跨过一次场景切变——某秒与前一秒带外 MAD ≥SCENE_CUT_MIN
    且切变两侧逐秒比值都 ≥RATIO_THR（黑卡赞助带的实证形态：
    卡与卡之间画面大变、带纹丝不动；提示文字只活在一张卡里，
    两侧凑不齐）。返回 (hit_blocks, distinct, sec_ratios)。
    """
    hits = []
    for b, med in enumerate(blocks):
        g = grad_strip(med, strip_det[0], strip_det[1])
        if grad_ratio(template, g) >= RATIO_THR:
            hits.append(b)
    sec_ratios = [grad_ratio(template, grad_strip(m, strip_det[0],
                                                  strip_det[1]))
                  for m in sec_meds]
    distinct = False
    for i in range(len(hits)):
        for j in range(i + 1, len(hits)):
            if outside_mad(blocks[hits[i]], blocks[hits[j]],
                           strip_det) >= SCENE_DIFF_MIN:
                distinct = True
                break
        if distinct:
            break
    if not distinct:
        for s in range(1, len(sec_meds)):
            if (sec_ratios[s - 1] >= SCENE_CUT_RATIO_MIN
                    and sec_ratios[s] >= SCENE_CUT_RATIO_MIN
                    and outside_mad(sec_meds[s - 1], sec_meds[s],
                                    strip_det) >= SCENE_CUT_MIN):
                distinct = True
                break
    return hits, distinct, sec_ratios


def _band_end_from_ratios(ratios):
    """逐秒比值 → 带尾秒：首个连续 2 秒低于阈值处；带持续到窗尾
    返回 None（上层按长带/水印口径处置）。"""
    present = [i for i, r in enumerate(ratios) if r >= RATIO_THR]
    if not present:
        return 0.0
    for s in range(present[0], len(ratios) - 1):
        if ratios[s] < RATIO_THR and ratios[s + 1] < RATIO_THR:
            return float(s)
    if ratios and ratios[-1] >= RATIO_THR:
        return None
    return float(len(ratios))


#: 逐行持续性的命中阈值：单行模板能量小、背景梯度的 min 匹配
#: 地板就有约 0.35（合成实测），必须用比 strip 级更高的阈值，
#: 静态行实测 ≈1.0、换位字幕行 0.31–0.38，分界取 0.50。
ROW_RATIO_THR = 0.50


def _row_persistence(sec_meds, template_rows, y0, y1, band_end):
    """条带逐行的持续性（字幕行隔离用）：每行在带窗秒数里与模板
    同位置同字形重合的比例。template_rows = 峰值块条带逐行梯度。"""
    secs = max(1, int(band_end))
    out = []
    for y in range(y0, y1):
        tpl = template_rows[y - y0]
        denom = sum(tpl)
        if denom < 5.0:
            # 模板行能量近零 = 该行在 5 秒中位帧里没有静态字（对白
            # 字幕逐秒换字会被中位冲淡）→ 不是赞助行，持续性记 0，
            # 不许拿微小分母算出假高比值。
            out.append(0.0)
            continue
        hits = 0
        for s in range(min(secs, len(sec_meds))):
            g = grad_strip(sec_meds[s], y, y + 1)
            if sum(min(a, b) for a, b in zip(g, tpl)) / denom \
                    >= ROW_RATIO_THR:
                hits += 1
        out.append(hits / min(secs, len(sec_meds) or 1))
    return out


def isolate_subtitle_rows(strip_det, dh, row_persist):
    """字幕行隔离门控：条带吃进底部字幕区时的拆分判定。

    条带下沿未进字幕区（底部 SUBTITLE_ZONE_FRAC）→ 原样返回。
    进了字幕区：从条带顶部向下找持续性断崖（赞助行全程都在、
    字幕行随对白换字）——断崖之上的连续高持续行是赞助行；
    拆出的赞助段须 ≥ 原条带 40% 且 ≥4 行，否则整条放弃（None）。
    """
    y0, y1 = int(strip_det[0]), int(strip_det[1])
    sub_top = int(dh * (1.0 - SUBTITLE_ZONE_FRAC))
    if y1 <= sub_top:
        return (y0, y1), False
    # 赞助行起点：首个高持续行（条带 ±1 扩边行本身无字、持续性
    # 必然低，不许让它把起点毒化）；断崖 = 起点后 ≥4 行处的首个
    # 低持续行，且须 2 行确认（单行凹陷是赞助字行间的空隙，不许
    # 误切；实测事故条的凹陷行 0.4 单行、字幕行 0.27 连片）。
    # 高/低界取 0.7：实测赞助行 ≈1.0、字幕行 0.27–0.55。
    start = None
    for i, p in enumerate(row_persist):
        if p >= 0.7:
            start = i
            break
    if start is None:
        return None, True
    cut = None
    for i in range(start + 4, len(row_persist)):
        if row_persist[i] < 0.7 and (
                row_persist[i] < 0.4
                or (i + 1 < len(row_persist)
                    and row_persist[i + 1] < 0.7)):
            cut = y0 + i
            break
    if cut is not None:
        top = y0 + start
        if cut - top < max(4, int(0.4 * (y1 - y0))):
            return None, True
        return (top, cut), True
    # 无断崖：字幕区内的行若全是高持续 → 整段是静态烧录本身
    # （赞助行本就贴底的实证形态），没有字幕行可伤，放行整段；
    # 有中间态行（0.4–0.7）→ 证明不了，fail-closed 放弃。
    zone_rows = [p for i, p in enumerate(row_persist)
                 if y0 + i >= sub_top]
    if zone_rows and all(p >= 0.7 for p in zone_rows):
        return (y0, y1), True
    return None, True


def detect_band(frames, dh, *, hint_zone=None):
    """全区扫描检测（frames = 2fps 低清灰度帧列）。

    返回 dict：ok=True 时带 strip_det（det 坐标行段）、band_end_sec
    （原始带尾，未外扩）、zone、conflict（与 hint 结论冲突）、
    narrowed（字幕隔离收窄过）、persistence 证据；ok=False 时
    reason ∈ no_band_found / gate_persistence /
    gate_subtitle_conflict / too_few_frames。
    """
    if len(frames) < 10:
        return {"ok": False, "reason": "too_few_frames"}
    blocks = block_medians(frames)
    if len(blocks) < 2:
        return {"ok": False, "reason": "too_few_frames"}
    sec_meds = second_medians(frames)
    candidates = []
    for zone, (zfrac0, zfrac1) in ZONES.items():
        zlo = int(zfrac0 * dh)
        zhi = int(zfrac1 * dh)
        found = _zone_candidate(blocks, zlo, zhi)
        if found is None:
            continue
        peak_b, run, score = found
        ly0 = max(zlo, run[0] - 1)
        ly1 = min(zhi, run[1] + 1)
        template = grad_strip(blocks[peak_b], ly0, ly1)
        hits, distinct, sec_ratios = _persistence(
            blocks, sec_meds, peak_b, (ly0, ly1), template)
        candidates.append({
            "zone": zone, "peak_block": peak_b, "score": score,
            "strip_det": (ly0, ly1), "template": template,
            "hit_blocks": hits, "distinct_scene": distinct,
            "sec_ratios": sec_ratios,
            "persistent": len(hits) >= 2 and distinct,
        })
    if not candidates:
        return {"ok": False, "reason": "no_band_found"}
    viable = [c for c in candidates if c["persistent"]]
    if not viable:
        return {"ok": False, "reason": "gate_persistence",
                "detail": "candidates=" + ",".join(
                    f"{c['zone']}:hits={len(c['hit_blocks'])}"
                    f":distinct={c['distinct_scene']}"
                    for c in candidates)}
    # 选择口径：hint 区有可持续候选时以 hint 为准（摸底 strip 是
    # 人工裁决的先验，本检测在该区自证成立就该信它——双带并存
    # 时按命中数选会选错主带，实证 1879267）；hint 区无候选或
    # 候选不过门控时，全区按峰值分最强者为准并记 conflict
    # （摸底标注错位的实证形态 1927459）。
    hint_cand = next((c for c in viable if c["zone"] == hint_zone),
                     None)
    if hint_cand is not None:
        chosen = hint_cand
        conflict = False
    else:
        chosen = max(viable, key=lambda c: c["score"])
        conflict = bool(hint_zone and hint_zone in ZONES)
    ly0, ly1 = chosen["strip_det"]
    template = chosen["template"]
    ratios = chosen["sec_ratios"]
    band_end = _band_end_from_ratios(ratios)
    result = {"ok": True, "zone": chosen["zone"],
              "strip_det": (ly0, ly1), "band_end_sec": band_end,
              "conflict": conflict, "narrowed": False,
              "peak_block": chosen["peak_block"],
              "hit_blocks": chosen["hit_blocks"],
              "grad_ratios": [round(r, 3) for r in ratios],
              "template": template}
    if band_end is None or band_end <= 0:
        return result
    # 字幕行隔离：条带吃进底部字幕区才触发（band_end 已知才算
    # 得出逐行持续性的时间窗）。
    sub_top = int(dh * (1.0 - SUBTITLE_ZONE_FRAC))
    if ly1 > sub_top:
        tpl_rows = [grad_strip(blocks[chosen["peak_block"]], y, y + 1)
                    for y in range(ly0, ly1)]
        row_persist = _row_persistence(sec_meds, tpl_rows, ly0, ly1,
                                       band_end)
        narrowed, triggered = isolate_subtitle_rows(
            (ly0, ly1), dh, row_persist)
        if triggered and narrowed is None:
            return {"ok": False, "reason": "gate_subtitle_conflict",
                    "detail": f"strip_det={ly0}-{ly1} sub_top={sub_top}"}
        if triggered:
            result["narrowed"] = tuple(narrowed) != tuple(
                result["strip_det"])
            result["strip_det"] = narrowed
            # 收窄后重算带尾（模板行段变了，旧比值不作数）
            tpl2 = grad_strip(blocks[chosen["peak_block"]],
                              narrowed[0], narrowed[1])
            ratios2 = [grad_ratio(tpl2, grad_strip(med, narrowed[0],
                                                    narrowed[1]))
                       for med in sec_meds]
            result["band_end_sec"] = _band_end_from_ratios(ratios2)
            result["grad_ratios"] = [round(r, 3) for r in ratios2]
            result["template"] = tpl2
    return result


def repair_end_sec(band_end, mouth) -> float:
    """实际修复窗尾：带尾外扩 TAIL_EXPAND_SEC、但不越口子。"""
    return min(float(mouth), float(band_end) + TAIL_EXPAND_SEC)


# ---------------------------------------------------------------- 背景分类与处理（试点定版）


def classify_background(frame_rgb, strip):
    """带区背景均匀性：钳制标准差（±18 钳制压掉文字像素贡献）。
    返回 (uniform, metrics)：uniform → fill，否则 blur。"""
    from PIL import ImageStat

    w, _h = frame_rgb.size
    y0, y1 = int(strip[0]), int(strip[1])
    crop = frame_rgb.convert("L").crop((0, y0, w, y1))
    hist = crop.histogram()
    total = sum(hist) or 1
    cum = 0
    med_val = 0
    for v, c in enumerate(hist):
        cum += c
        if cum >= total / 2:
            med_val = v
            break
    lo = max(0, med_val - 18)
    hi = min(255, med_val + 18)
    lut = [min(max(v, lo), hi) for v in range(256)]
    clamped = crop.point(lut)
    std_clamped = ImageStat.Stat(clamped).stddev[0]
    std_raw = ImageStat.Stat(crop).stddev[0]
    return std_clamped < 15.0, {
        "strip_median": med_val,
        "std_clamped": round(std_clamped, 2),
        "std_raw": round(std_raw, 2)}


def _edge_blend(frame, y0e, y1e):
    """上下边缘行延展＋纵向渐变混合（均匀背景的填充底图）。"""
    from PIL import Image

    w, h = frame.size
    sh = y1e - y0e
    top = frame.crop((0, max(0, y0e - 1), w, y0e)).resize((w, sh))
    bot = frame.crop((0, y1e, w, min(h, y1e + 1))).resize((w, sh))
    ramp = Image.new("L", (1, sh))
    ramp.putdata([int(255 * i / max(1, sh - 1)) for i in range(sh)])
    ramp = ramp.resize((w, sh))
    return Image.composite(bot, top, ramp)


def process_frame(frame, strip, method):
    """只动条带（含 ±6 羽化）：fill 填充 / blur 强模糊；带外零改动。

    method ∈ fill / blur（记账口径 self_fill / self_blur 由上层拼）。
    """
    from PIL import Image, ImageFilter

    from backend.emby_server import cms_graft as graft

    w, h = frame.size
    y0e = max(0, int(strip[0]) - 6)
    y1e = min(h, int(strip[1]) + 6)
    strip_img = frame.crop((0, y0e, w, y1e))
    sh = y1e - y0e
    if method == "fill":
        edge = _edge_blend(frame, y0e, y1e)
        wash = strip_img.filter(
            ImageFilter.GaussianBlur(radius=max(24, sh)))
        new_strip = Image.blend(edge, wash, 0.35)
    else:  # blur：两遍高斯（半径=带高×0.5），目标赞助字不可辨读
        r = max(10.0, sh * 0.5)
        new_strip = strip_img.filter(ImageFilter.GaussianBlur(radius=r))
        new_strip = new_strip.filter(ImageFilter.GaussianBlur(radius=r))
    replaced = frame.copy()
    replaced.paste(new_strip, (0, y0e))
    mask = graft._feather_mask(w, y0e, y1e, h)
    return Image.composite(replaced, frame, mask)


# ---------------------------------------------------------------- 解码/编码（限线程版，试点口径）


def decode_frames_t(paths, *, width, height, fps, gray=True):
    """cms_graft.decode_frames 的限线程版（-threads 2），语义一致：
    整窗拼接解码、解码顺序逐帧（禁按秒抽帧）。"""
    from PIL import Image

    from backend.emby_server import cms_graft as graft

    if not paths:
        return
    listfile = None
    if len(paths) > 1:
        import tempfile

        fd, listfile = tempfile.mkstemp(
            prefix="selff_concat_", suffix=".txt", dir="/tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for p in paths:
                fh.write(f"file '{p}'\n")
        src = ["-f", "concat", "-safe", "0", "-i", listfile]
    else:
        src = ["-i", str(paths[0])]
    pix = "gray" if gray else "rgb24"
    vf = f"scale={width}:{height},fps={fps}"
    cmd = (["ffmpeg", "-v", "error", "-threads", "2"] + src +
           ["-vf", vf, "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"])
    mode = "L" if gray else "RGB"
    frame_bytes = width * height * (1 if gray else 3)
    try:
        for buf in graft._frame_iter(cmd, frame_bytes):
            yield Image.frombytes(mode, (width, height), buf)
    finally:
        if listfile:
            try:
                os.unlink(listfile)
            except OSError:
                pass


def _start_encoder_t(out_path, seg_path, info):
    """cms_graft._start_encoder 的限线程版：rawvideo 进、TS 出、
    音频从原段整段 copy（零重编）；编码参数与源分段对齐。"""
    width, height = int(info["width"]), int(info["height"])
    rate = str(info.get("fps_rate") or f"{float(info['fps']):.4f}")
    cmd = ["ffmpeg", "-y", "-v", "error", "-threads", "2",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", rate, "-i", "pipe:0"]
    if info.get("has_audio"):
        cmd += ["-i", str(seg_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-threads", "2", "-f", "mpegts", str(out_path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _grab_frame(seg_paths, idx, *, width, height, fps, gray=False):
    """整窗解码数帧取第 idx 帧（解码顺序口径，禁 -ss）。"""
    gen = decode_frames_t(seg_paths, width=width, height=height,
                          fps=fps, gray=gray)
    for i, fr in enumerate(gen):
        if i == idx:
            return fr
    return None


# ---------------------------------------------------------------- 修后复检


def post_check(orig_paths, rep_paths, info, strip_full, template,
               band_end, repair_end):
    """修后复检（修完再跑一次检测口径，fail-closed）：

    - 带还在：修复帧逐秒 2D 梯度比（对原模板）任一秒 ≥RATIO_THR
      （限带窗内秒数）→ 不过；
    - 带外损伤：原/修同帧带外 MAD 均值 >POST_OUT_MAD_MAX → 不过。
    返回 (ok, detail dict)。
    """
    fps = float(info["fps"])
    w, h = int(info["width"]), int(info["height"])
    dh = max(2, int(round(DET_W * h / max(1, w))))
    strip_det = (int(strip_full[0] * dh / h),
                 int(strip_full[1] * dh / h))
    rep_frames = list(decode_frames_t(
        rep_paths, width=DET_W, height=dh, fps=2, gray=True))
    if len(rep_frames) < 4:
        return False, {"reason": "post_check_no_frames"}
    sec_meds = second_medians(rep_frames)
    win_secs = max(1, int(repair_end))
    ratios = []
    for s in range(min(win_secs, len(sec_meds))):
        ratios.append(grad_ratio(
            template, grad_strip(sec_meds[s], strip_det[0],
                                 strip_det[1])))
    if ratios and max(ratios) >= RATIO_THR:
        return False, {"reason": "post_check_band_remains",
                       "max_ratio": round(max(ratios), 3),
                       "ratios": [round(r, 3) for r in ratios]}
    # 带外损伤：带中点与带尾前两帧同帧对照（480 口径）
    mads = []
    for t in sorted({max(0.5, band_end * 0.5),
                     max(0.5, band_end - 1.0)}):
        idx = int(round(t * fps))
        before = _grab_frame(orig_paths, idx, width=DET_W, height=dh,
                             fps=fps, gray=True)
        after = _grab_frame(rep_paths, idx, width=DET_W, height=dh,
                            fps=fps, gray=True)
        if before is None or after is None:
            continue
        mads.append(outside_mad(before, after, strip_det))
    if mads and sum(mads) / len(mads) > POST_OUT_MAD_MAX:
        return False, {"reason": "post_check_outside_damage",
                       "outside_mad": [round(m, 2) for m in mads]}
    return True, {"max_ratio": round(max(ratios), 3) if ratios else 0.0,
                  "outside_mad": [round(m, 2) for m in mads]}


# ---------------------------------------------------------------- 管线


def _encode_window(seg_paths, seg_durs, info, strip, repair_end,
                   method, out_dir: Path):
    """逐段重编码受影响分段（整窗拼接解码、帧预算切分，同 graft）。
    返回段表 [{idx, file, frames, bytes}]；失败抛 GraftError。"""
    from backend.emby_server import cms_graft as graft

    fps = float(info["fps"])
    w, h = int(info["width"]), int(info["height"])
    affected = []
    t_acc = 0.0
    for i, d in enumerate(seg_durs):
        if t_acc < repair_end - 0.05 and i < len(seg_paths):
            affected.append(i)
        t_acc += float(d or 0.0)
    if not affected:
        raise graft.GraftError("no_affected_segments")
    aff_paths = [seg_paths[i] for i in affected]
    gen = decode_frames_t(aff_paths, width=w, height=h, fps=fps,
                          gray=False)
    out_segs = []
    gidx = -1
    for pos, seg_i in enumerate(affected):
        tmp_out = out_dir / f"seg_{seg_i:03d}.ts.tmp"
        enc = _start_encoder_t(tmp_out, seg_paths[seg_i], info)
        if pos < len(affected) - 1:
            budget = max(1, int(round(float(seg_durs[seg_i]) * fps)))
        else:
            budget = None  # 末段吃到流尽（同 graft 口径）
        n_here = 0
        while budget is None or n_here < budget:
            fr = next(gen, None)
            if fr is None:
                break
            gidx += 1
            t = gidx / fps
            if t <= repair_end + 0.25:
                fr = process_frame(fr, strip, method)
            enc.stdin.write(fr.tobytes())
            n_here += 1
        try:
            enc.stdin.close()
        except BrokenPipeError:
            pass
        rc = enc.wait()
        if rc != 0 or n_here == 0:
            tmp_out.unlink(missing_ok=True)
            raise graft.GraftError(f"encode_failed seg={seg_i} rc={rc}")
        final = out_dir / f"seg_{seg_i:03d}.ts"
        os.replace(tmp_out, final)
        out_segs.append({"idx": seg_i, "file": final.name,
                         "frames": n_here,
                         "bytes": final.stat().st_size})
    return out_segs, affected


def process_self(db, mapping, *, mouth, stats=None, verdict=None):
    """一条映射的自帧全流程（探测→门控→处理→复检→段级自检）。

    返回 outcome dict（口径同 cms_graft.process_pair）：status ∈
    repaired / skipped / failed，repaired 时带 segments / strip /
    band_end_sec / method（self_fill 或 self_blur）。只写产物
    文件、不写库（记账由 process_candidate_self 统一落）。
    """
    import shutil
    import tempfile

    from backend.emby_server import cms_graft as graft

    work = Path(tempfile.mkdtemp(prefix=f"selff_{mapping.id}_",
                                  dir="/tmp"))
    try:
        url, headers = graft.resolve_stream(db, mapping)
        dsegs, dinit, _murl, dkey = graft.load_media_playlist(
            url, headers, source_id=mapping.source_id, db=db,
            stats=stats)
        if dinit:
            return {"status": "skipped",
                    "reason": "unsupported_init_segment"}
        dkey_bytes = graft.fetch_key_bytes(
            dkey, headers, source_id=mapping.source_id, db=db,
            stats=stats)
        horizon = float(mouth) + 25.0
        dpaths, _tot = graft._download_segments(
            dsegs, headers, source_id=mapping.source_id, db=db,
            stats=stats, work_dir=work, prefix="seg",
            cover_sec=horizon, key_info=dkey, key_bytes=dkey_bytes)
        if not dpaths:
            return {"status": "failed", "reason": "fetch_failed",
                    "detail": "no segments downloaded"}
        info = graft.video_info(dpaths[0])
        if info["codec"] != "h264":
            return {"status": "skipped", "reason": "codec_unsupported",
                    "detail": f"codec={info['codec']}"}
        w, h = int(info["width"]), int(info["height"])
        dh = max(2, int(round(DET_W * h / max(1, w))))
        frames = list(decode_frames_t(
            dpaths, width=DET_W, height=dh, fps=2, gray=True))
        hint_zone = None
        if verdict and str(verdict.get("strip") or "") in ZONES:
            hint_zone = str(verdict["strip"])
        det = detect_band(frames, dh, hint_zone=hint_zone)
        if not det.get("ok"):
            status = ("failed" if det["reason"] == "too_few_frames"
                      else "skipped")
            return {"status": status, "reason": det["reason"],
                    "detail": det.get("detail", "")[:200]}
        band_end = det["band_end_sec"]
        strip_full = (int(det["strip_det"][0] * h / dh),
                      int(det["strip_det"][1] * h / dh))
        base = {"band_family": "", "strip": {
            "y0": strip_full[0], "y1": strip_full[1], "h": h}}
        if band_end is None:
            return {"status": "skipped",
                    "reason": "watermark_or_overlong", **base}
        if band_end <= 0:
            return {"status": "skipped", "reason": "no_band_found",
                    **base}
        if band_end > mouth:
            return {"status": "skipped", "reason": "band_too_long",
                    "band_end_sec": band_end, **base}
        repair_end = repair_end_sec(band_end, mouth)
        # 背景分类：峰值块时刻的全分辨率帧
        fps = float(info["fps"])
        peak_t = det["peak_block"] * 5.0 + 2.5
        fr_peak = _grab_frame(dpaths, int(peak_t * fps), width=w,
                              height=h, fps=fps, gray=False)
        if fr_peak is None:
            return {"status": "failed", "reason": "probe_failed",
                    "detail": "peak frame missing"}
        uniform, bg_metrics = classify_background(fr_peak, strip_full)
        method = "fill" if uniform else "blur"
        out_dir = graft.mapping_dir(mapping.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("seg_*.ts"):
            old.unlink(missing_ok=True)
        seg_durs = [float(d or 0.0) for d, _u in dsegs]
        segments, _affected = _encode_window(
            dpaths, seg_durs, info, strip_full, repair_end, method,
            out_dir)
        # 段级自检（同 graft：时长与原段帧数口径对齐、音频一致）
        for i, seg in enumerate(segments):
            seg["dur"] = float(dsegs[seg["idx"]][0] or 0.0)
            vinfo = graft.video_info(out_dir / seg["file"])
            expect = seg["frames"] / float(info["fps"])
            oinfo = graft.video_info(dpaths[seg["idx"]])
            if (abs(vinfo["duration"] - expect) > 0.75
                    or vinfo["has_audio"] != oinfo["has_audio"]):
                for seg2 in segments:
                    (out_dir / seg2["file"]).unlink(missing_ok=True)
                return {"status": "failed",
                        "reason": "post_check_failed",
                        "detail": f"seg={seg['idx']} "
                                  f"dur={vinfo['duration']:.2f} "
                                  f"expect={expect:.2f}"}
        # 修后复检（带还在/带外损伤超阈 → 判失败、不下发）
        rep_paths = [out_dir / s["file"] for s in segments]
        ok, pc_detail = post_check(
            dpaths, rep_paths, info, strip_full, det["template"],
            band_end, repair_end)
        if not ok:
            for seg2 in segments:
                (out_dir / seg2["file"]).unlink(missing_ok=True)
            return {"status": "failed", "reason": "post_check_failed",
                    "band_end_sec": band_end,
                    "detail": json.dumps(pc_detail,
                                         ensure_ascii=False)[:200],
                    **base}
        detail = {"method": method, "bg": bg_metrics,
                  "zone": det["zone"], "conflict": det["conflict"],
                  "narrowed": det["narrowed"],
                  "hit_blocks": det["hit_blocks"],
                  "repair_end_sec": round(repair_end, 1),
                  "post_check": pc_detail}
        return {"status": "repaired", "reason": "",
                "segments": segments, "band_end_sec": band_end,
                "method": f"self_{method}",
                "detail": json.dumps(detail, ensure_ascii=False)[:500],
                **base}
    except graft.FetchError as exc:
        return {"status": "failed", "reason": "fetch_failed",
                "detail": str(exc)[:200]}
    except graft.GraftError as exc:
        reason = str(exc).split(" ", 1)[0] or "gate_failed"
        return {"status": "failed", "reason": reason,
                "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — 单条失败不许带崩整批
        from backend.emby_server.upstream_playback import (
            UpstreamPlaybackError)

        if isinstance(exc, UpstreamPlaybackError):
            return {"status": "failed", "reason": "fetch_failed",
                    "detail": f"resolve {exc.code}"}
        logger.warning("cms_self_frame process error mapping=%s",
                       mapping.id, exc_info=True)
        return {"status": "failed", "reason": "process_error",
                "detail": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def process_candidate_self(db, candidate, *, mouth, stats=None,
                           verdict=None):
    """工人单条全流程（自帧线）：修复 → 验证，终局状态落库。

    与借帧线 process_candidate 的区别：不找干净兄弟（自帧不需要
    供体）；记账 method=self_fill/self_blur；验证复用
    cms_graft.verify_repair（清单组装＋段解码＋带中点帧复检，
    无兄弟时自动跳过兄弟比对支路）。返回 outcome dict 供跑批统计。
    """
    from backend.emby_server import cms_graft as graft
    from backend.emby_server import models as em

    mapping_id = int(candidate["mapping_id"])
    existing = graft.get_repair(db, mapping_id)
    if existing is not None and existing.status == "verified":
        return {"mapping_id": mapping_id, "status": "verified",
                "reason": "already", "method": existing.method,
                "heat": candidate.get("heat")}
    dirty = (db.query(em.UpstreamItemMapping)
             .filter(em.UpstreamItemMapping.id == mapping_id).first())
    if dirty is None:
        return {"mapping_id": mapping_id, "status": "failed",
                "reason": "mapping_gone", "heat": candidate.get("heat")}
    graft.upsert_repair(db, mapping_id=mapping_id,
                        local_item_id=dirty.local_item_id,
                        source_id=dirty.source_id, status="running",
                        method="self", mouth=mouth,
                        worker_version=WORKER_VERSION)
    db.commit()
    outcome = process_self(db, dirty, mouth=mouth, stats=stats,
                           verdict=verdict)
    status = outcome["status"]
    graft.upsert_repair(
        db, mapping_id=mapping_id, local_item_id=dirty.local_item_id,
        source_id=dirty.source_id, status=status,
        reason=outcome.get("reason", ""),
        segments=outcome.get("segments"),
        band_end_sec=outcome.get("band_end_sec"),
        band_family=outcome.get("band_family", ""),
        strip=outcome.get("strip"),
        method=outcome.get("method", "self"), mouth=mouth,
        detail=outcome.get("detail", ""),
        worker_version=WORKER_VERSION)
    db.commit()
    result = {"mapping_id": mapping_id, "status": status,
              "reason": outcome.get("reason", ""),
              "band_end_sec": outcome.get("band_end_sec"),
              "method": outcome.get("method", "self"),
              "heat": candidate.get("heat")}
    if status == "repaired":
        verified = graft.verify_repair(db, mapping_id, stats=stats)
        result["status"] = verified["status"]
        result["reason"] = verified.get("reason", "")
    return result
