# -*- coding: utf-8 -*-
"""115 分片尺寸规则残留回归（_determine_partsize）。

本文件原为 115 分片上传整套：multipart_upload/upload_token/
分片续传与相关开关测试已随 2026-11-02 夸克/115 绑盘线删除
（二期）一并删除——分片上传方法与开关均已删。
现只留 _determine_partsize 一项：该函数未删（transfer_manifest
persist_delivery 落盘时用它算 part↔块映射，属通用交付尺寸规则，
非上传专属），其尺寸数学仍需回归钉住。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import transfer115


def test_determine_partsize_math():
    f = transfer115._determine_partsize
    min10 = 10 * 1024 * 1024
    assert f(0) == min10
    assert f(123) == min10
    # 恰在上限内：分片数不超 10000 且大小是下限的 2 的幂倍
    big = 5 * 1024 ** 3 + 1
    ps = f(big)
    assert ps >= min10 and (ps // min10) & (ps // min10 - 1) == 0
    assert -(-big // ps) <= 10000
    # p115oss 原算法口径（下限 100 KiB）逐值对照
    assert f(100 * 1024, min_partsize=100 * 1024) == 100 * 1024
    assert f(100 * 1024 * 10000 + 1,
             min_partsize=100 * 1024) == 200 * 1024
    assert f(100 * 1024 * 10000,
             min_partsize=100 * 1024) == 100 * 1024
