"""借帧修复工人入口（独立容器跑批，2026-11-03 第一批）。

用法（容器内）：``python -m backend.emby_server.cms_graft_worker
[--limit 500] [--verdicts /data/cms_graft/verdicts_seed.jsonl]
[--pool 4000] [--dry-select] [--method graft|self]``

``--method self`` 走自帧修复线（cms_self_frame，2026-11-04 主
路线）：不找干净兄弟、队列口径不变；熔断换试点收紧版
（cms_self_frame.host_ok_strict：内存 <1.2GB / load1 >6 /
health TTFB >1s 连二），其余记账/续跑/家宽信号量与借帧线共用。

纪律（派单口径）：

- 只在本工人进程里跑（独立容器、同镜像只挂数据卷），不进 api
  进程；进程以低优先级运行（nice 10）。
- 有界并发：总并发读配置 cms_graft_concurrency（默认 2）；涉
  家宽出口的条目另受 cms_graft_home_concurrency（默认 1）信号量
  约束——家宽只抓片头分段、流量逐条记账（RouteStats 快照进
  summary）。
- 熔断：每条开工前查宿主机可用内存/负载（cms_graft.host_ok：
  可用 <1.5GB 或负载 >40 即停）；触发后等候复查（30 秒 ×10），
  仍不达标就收工——已落库的逐条状态即断点，重跑自动续。
- 全局暂停：配置 cms_graft_enabled 关 → 工人收工、播放下发闸
  同步关闭（同一键，见 cms_graft.graft_enabled）。
- 规模：入选（有干净兄弟、进入管线判定）达 --limit 即停，不许
  自行扩全量；跑批留档落产物根 runs/<时间戳>/（队列、逐条
  结果、summary），不放容器 /tmp。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("cms_graft_worker")


def partition_candidates(queue, existing_status: dict):
    """按已落库状态分拣候选（断点续跑口径）：

    - 无行 → todo；
    - ``running`` → todo（上一轮进程被杀/崩溃的残留：工人单跑
      纪律下不存在另一个活工人，running 只可能是死残留，必须
      重处理，不许静默丢弃——第一批第二轮实证丢过 30 条）；
    - ``repaired`` → verify_only（只补验证，不重修）；
    - 其余（verified/skipped/failed/verify_failed）为终局，跳过。
    """
    todo, verify_only = [], []
    for cand in queue:
        status = existing_status.get(int(cand["mapping_id"]))
        if status is None or status == "running":
            todo.append(cand)
        elif status == "repaired":
            verify_only.append(cand)
    return todo, verify_only


def _load_verdicts(path):
    verdicts = {}
    if not path:
        return verdicts
    p = Path(path)
    if not p.exists():
        logger.warning("verdicts seed not found: %s", path)
        return verdicts
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            mid = row.get("mapping_id")
            if mid is not None:
                verdicts[int(mid)] = row
    logger.info("verdicts seed loaded: %d rows", len(verdicts))
    return verdicts


def _host_wait(max_checks=10, sleep_sec=30, strict=False):
    """熔断等候：宿主状态恢复返回 True；等满仍不达标返回 False。

    strict=True（自帧线）：阈值换试点收紧版，且 health TTFB
    连续两次 >1s 也算未恢复（单次抖动不误杀，计数在本函数内维护）。
    """
    from backend.emby_server import cms_graft

    bad_ttfb = 0
    for _ in range(max_checks):
        mem_kb, load1 = cms_graft.host_state()
        if strict:
            from backend.emby_server import cms_self_frame

            ok = cms_self_frame.host_ok_strict(mem_kb, load1)
            if ok:
                if cms_self_frame.health_ttfb() > 1.0:
                    bad_ttfb += 1
                    ok = bad_ttfb < 2
                else:
                    bad_ttfb = 0
        else:
            ok = cms_graft.host_ok(mem_kb, load1)
        if ok:
            return True
        logger.warning("circuit breaker: mem_available=%dKB load1=%.1f, "
                       "waiting", mem_kb, load1)
        time.sleep(sleep_sec)
    return False


def _host_ok_now(strict=False):
    """派活前的即时熔断判定（口径与 _host_wait 同源）。"""
    from backend.emby_server import cms_graft

    mem_kb, load1 = cms_graft.host_state()
    if strict:
        from backend.emby_server import cms_self_frame

        return cms_self_frame.host_ok_strict(mem_kb, load1)
    return cms_graft.host_ok(mem_kb, load1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cms_graft_worker")
    parser.add_argument("--limit", type=int, default=None,
                        help="入选上限（默认读配置 cms_graft_batch_limit）")
    parser.add_argument("--pool", type=int, default=4000,
                        help="候选池大小（按热度取前 N 条映射）")
    parser.add_argument("--verdicts", default=None,
                        help="摸底判定底稿 jsonl（现成数据复用、不重测）")
    parser.add_argument("--dry-select", action="store_true",
                        help="只建队列+兄弟预检统计，不真修")
    parser.add_argument("--method", choices=("graft", "self"),
                        default="graft",
                        help="修复线：graft=借帧（默认，存量行为）；"
                             "self=自帧处理（cms_self_frame）")
    args = parser.parse_args(argv)
    strict_breaker = args.method == "self"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    try:
        os.nice(10)
    except OSError:
        pass

    from backend.database import SessionLocal
    from backend.emby_server import cms_graft
    from backend.emby_server import upstream_playback as up

    db = SessionLocal()
    try:
        if not cms_graft.graft_enabled(db):
            logger.info("cms_graft disabled by config; exit")
            return 0
        limit = args.limit or cms_graft.batch_limit(db)
        conc = cms_graft.concurrency(db)
        home_conc = cms_graft.home_concurrency(db)
        home_ids = cms_graft.home_source_ids(db)
        queue = cms_graft.build_candidate_queue(
            db, demoted_source_ids=up.demoted_source_ids(db),
            limit=args.pool, heat_days_n=cms_graft.heat_days(db))
        # 已有终局结论的跳过（断点续跑口径见 partition_candidates）。
        from backend.emby_server import models as _em

        existing_status = {
            int(mid): status for mid, status in (
                db.query(_em.CmsGraftRepair.mapping_id,
                         _em.CmsGraftRepair.status).all())}
        todo, verify_only = partition_candidates(queue, existing_status)
        logger.info("queue=%d todo=%d verify_only=%d limit=%d conc=%d "
                    "home_conc=%d", len(queue), len(todo),
                    len(verify_only), limit, conc, home_conc)
    finally:
        db.close()

    verdicts = _load_verdicts(args.verdicts)
    run_dir = (cms_graft.store_root() / "runs"
               / datetime.now().strftime("%Y%m%d_%H%M%S"))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "queue.json").write_text(
        json.dumps(queue, ensure_ascii=False, indent=1),
        encoding="utf-8")
    if args.dry_select:
        logger.info("dry-select done: queue=%d todo=%d verify_only=%d",
                    len(queue), len(todo), len(verify_only))
        return 0

    stats = cms_graft.RouteStats()
    results_path = run_dir / "results.jsonl"
    results_lock = threading.Lock()
    counters = {"entered": 0, "processed": 0}
    counters_lock = threading.Lock()
    home_sem = threading.Semaphore(home_conc)
    stop_reason = {"value": ""}

    def _record(result, wall_sec, cpu_sec):
        result = dict(result)
        result["wall_sec"] = round(wall_sec, 2)
        result["cpu_sec"] = round(cpu_sec, 2)
        result["ts"] = datetime.now().isoformat(timespec="seconds")
        with results_lock:
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    def _process(cand):
        from backend.database import SessionLocal as _SL

        session = _SL()
        try:
            if strict_breaker:
                # 自帧线：不需要干净兄弟，进管线即计数（入选与否
                # 由检测+三门控在管线内 fail-closed 判定）。
                from backend.emby_server import cms_self_frame

                home_involved = bool(
                    cand["source_id"] in home_ids
                    or cand["source_id"]
                    in cms_graft.HOME_FALLBACK_SOURCE_IDS)
                with counters_lock:
                    if counters["entered"] >= limit:
                        return None
                    counters["entered"] += 1
                wall0 = time.time()
                cpu0 = time.process_time()
                try:
                    def _run_self():
                        return cms_self_frame.process_candidate_self(
                            session, cand,
                            mouth=cms_graft.mouth_sec(session),
                            stats=stats,
                            verdict=verdicts.get(cand["mapping_id"]))

                    if home_involved:
                        with home_sem:
                            result = _run_self()
                    else:
                        result = _run_self()
                except Exception as exc:  # noqa: BLE001 — 单条崩不许丢账
                    logger.warning("candidate crashed mapping=%s: %s",
                                   cand["mapping_id"], exc,
                                   exc_info=True)
                    try:
                        cms_graft.upsert_repair(
                            session, mapping_id=cand["mapping_id"],
                            local_item_id=cand["local_item_id"],
                            source_id=cand["source_id"], status="failed",
                            reason="process_error", method="self",
                            detail=f"{type(exc).__name__}: {exc}"[:200])
                        session.commit()
                    except Exception:  # noqa: BLE001
                        session.rollback()
                    result = {"mapping_id": cand["mapping_id"],
                              "status": "failed",
                              "reason": "process_error",
                              "heat": cand.get("heat")}
                _record(result, time.time() - wall0,
                        time.process_time() - cpu0)
                with counters_lock:
                    counters["processed"] += 1
                return result
            sibling = cms_graft.find_sibling(
                session, cand["mapping_id"], cms_graft.CLEAN_SOURCE_IDS)
            entered = sibling is not None
            home_involved = bool(
                cand["source_id"] in home_ids
                or cand["source_id"] in cms_graft.HOME_FALLBACK_SOURCE_IDS
                or (sibling is not None
                    and sibling.source_id in home_ids))
            if entered:
                with counters_lock:
                    if counters["entered"] >= limit:
                        return None
                    counters["entered"] += 1
            wall0 = time.time()
            cpu0 = time.process_time()
            try:
                if home_involved:
                    with home_sem:
                        result = cms_graft.process_candidate(
                            session, cand,
                            mouth=cms_graft.mouth_sec(session),
                            stats=stats,
                            verdict=verdicts.get(cand["mapping_id"]))
                else:
                    result = cms_graft.process_candidate(
                        session, cand, mouth=cms_graft.mouth_sec(session),
                        stats=stats,
                        verdict=verdicts.get(cand["mapping_id"]))
            except Exception as exc:  # noqa: BLE001 — 单条崩不许丢账
                logger.warning("candidate crashed mapping=%s: %s",
                               cand["mapping_id"], exc, exc_info=True)
                try:
                    cms_graft.upsert_repair(
                        session, mapping_id=cand["mapping_id"],
                        local_item_id=cand["local_item_id"],
                        source_id=cand["source_id"], status="failed",
                        reason="process_error",
                        detail=f"{type(exc).__name__}: {exc}"[:200])
                    session.commit()
                except Exception:  # noqa: BLE001 — 补账再失败只剩日志
                    session.rollback()
                result = {"mapping_id": cand["mapping_id"],
                          "status": "failed", "reason": "process_error",
                          "heat": cand.get("heat")}
            _record(result, time.time() - wall0,
                    time.process_time() - cpu0)
            with counters_lock:
                counters["processed"] += 1
            return result
        finally:
            session.close()

    # 先补验证（repaired 遗留），再跑新队列
    for cand in verify_only:
        session = SessionLocal()
        try:
            out = cms_graft.verify_repair(
                session, cand["mapping_id"], stats=stats)
            _record({"mapping_id": cand["mapping_id"], **out}, 0.0, 0.0)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = {}
        idx = 0
        while idx < len(todo):
            with counters_lock:
                entered = counters["entered"]
            if entered >= limit:
                stop_reason["value"] = "limit_reached"
                break
            # 熔断与暂停开关：派活前逐个查（已派的让它跑完）
            probe = SessionLocal()
            try:
                if not cms_graft.graft_enabled(probe):
                    stop_reason["value"] = "paused_by_config"
                    break
            finally:
                probe.close()
            if not _host_ok_now(strict=strict_breaker):
                if not _host_wait(strict=strict_breaker):
                    stop_reason["value"] = "circuit_breaker"
                    break
            fut = pool.submit(_process, todo[idx])
            futures[fut] = todo[idx]["mapping_id"]
            idx += 1
            # 控制在途数量，避免一次性把全池压进执行器后熔断失灵
            while len(futures) >= conc * 2:
                done = next(as_completed(list(futures)))
                futures.pop(done, None)
                if done.exception() is not None:
                    logger.warning("candidate crashed: %s",
                                   done.exception())
        for fut in as_completed(list(futures)):
            if fut.exception() is not None:
                logger.warning("candidate crashed: %s", fut.exception())

    # 汇总（逐条结果文件即明细，这里只出分布与账）
    summary = {"run_dir": str(run_dir), "method": args.method,
               "stop_reason": stop_reason["value"]
               or "queue_exhausted", "counters": dict(counters),
               "route_stats": stats.snapshot(),
               "finished_at": datetime.now().isoformat(timespec="seconds")}
    outcomes: dict[str, int] = {}
    reasons: dict[str, int] = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            outcomes[row.get("status", "?")] = \
                outcomes.get(row.get("status", "?"), 0) + 1
            key = f"{row.get('status')}/{row.get('reason', '')}"
            reasons[key] = reasons.get(key, 0) + 1
    summary["outcomes"] = outcomes
    summary["reasons"] = reasons
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1),
        encoding="utf-8")
    logger.info("run done: %s", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
