"""去重落档服务层：钥匙策略、解析结果造数、落档校验与后台去重。"""
import os
import threading
import time

from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from catalog.models import (
    ArchiveBatch,
    ArchiveEntry,
    DedupPolicy,
    ParseRecord,
)

DISPOSITION_NAMES = dict(DedupPolicy.DISPOSITIONS)


# ---------- 钥匙策略 ----------

def get_policy():
    return DedupPolicy.objects.order_by("id").first()


def write_policy(disposition):
    """只许定一种处置写进策略；写完当天不许换。"""
    if disposition not in DISPOSITION_NAMES:
        return False, "处置只许定一种：跳过并计数，或整批失败并退回读盘前快照"
    today = timezone.localdate()
    policy = get_policy()
    if policy and policy.decided_on >= today:
        return False, "钥匙策略写完当天不许换"
    if policy:
        policy.disposition = disposition
        policy.decided_on = today
        policy.save(update_fields=["disposition", "decided_on", "updated_at"])
    else:
        DedupPolicy.objects.create(disposition=disposition, decided_on=today)
    return True, f"钥匙策略已写入：{DISPOSITION_NAMES[disposition]}"


# ---------- 解析结果（读盘收尾时造数，每条带去重钥匙） ----------

def generate_rows(job):
    """确定性造解析结果：每第 7 条撞首条钥匙；index_json 第 3 条钥匙为空。"""
    count = 12 + (job.id * 2) % 9
    rows = []
    for seq in range(1, count + 1):
        key = f"{job.code}-{seq:03d}"
        if seq % 7 == 0:
            key = f"{job.code}-001"  # 批内撞钥样例
        if job.code == "index_json" and seq == 3:
            key = ""  # 空钥匙样例
        rows.append((seq, key, f"{job.code} 第{seq}条字段包"))
    return rows


def finalize_read(job):
    """读盘收尾：重建解析结果，任务进入待落档。"""
    job.records.all().delete()
    rows = generate_rows(job)
    ParseRecord.objects.bulk_create(
        ParseRecord(job=job, seq=seq, dedup_key=key, payload=payload)
        for seq, key, payload in rows
    )
    job.parsed = len(rows)
    job.landed = 0
    job.parse_epoch += 1
    job.status = "已解析待入库"
    job.save(update_fields=["parsed", "landed", "parse_epoch", "status"])


# ---------- 去重落档 ----------

def start_archive(job, disposition, user):
    """开去重落档。校验顺序：收尾 → 策略 → 处理法 → 空钥匙 → 同批争抢。"""
    if job.status != "已解析待入库":
        return False, "读盘未收尾，禁止开去重落档", None
    policy = get_policy()
    if policy is None:
        return False, "钥匙策略未写，请先到钥匙页定处置", None
    if disposition != policy.disposition:
        return False, "去重处理写歪了", None
    if job.records.filter(dedup_key="").exists():
        return False, "钥匙字段空着禁止落档", None
    try:
        with transaction.atomic():
            batch = ArchiveBatch.objects.create(
                job=job,
                parse_epoch=job.parse_epoch,
                batch_seq=job.batches.count() + 1,
                disposition=disposition,
                total=job.records.count(),
                created_by=user,
            )
    except IntegrityError:
        return False, "同一批落档两人争抢，只成一笔（只记先到）", None
    if os.environ.get("DEDUP_SYNC") == "1":
        run_dedup(batch.id)
    else:
        threading.Thread(target=_run_dedup_async, args=(batch.id,), daemon=True).start()
    return True, f"落档批次 #{batch.batch_seq} 已开，去重后台进行中", batch


def _run_dedup_async(batch_id):
    try:
        run_dedup(batch_id)
    finally:
        close_old_connections()


def run_dedup(batch_id):
    """后台去重：撞钥跳过并计数，或整批失败退回读盘前快照。"""
    batch = None
    for _ in range(5):  # 等开批事务落盘
        batch = ArchiveBatch.objects.filter(id=batch_id).first()
        if batch:
            break
        time.sleep(0.05)
    if batch is None:
        return
    job = batch.job
    try:
        records = list(job.records.order_by("seq"))
        seen = set(ArchiveEntry.objects.values_list("dedup_key", flat=True))
        collisions, entries = [], []
        for rec in records:
            if rec.dedup_key in seen:
                collisions.append(rec)  # 后到者撞钥
                continue
            seen.add(rec.dedup_key)
            entries.append(ArchiveEntry(
                batch=batch, job=job, seq=rec.seq,
                dedup_key=rec.dedup_key, payload=rec.payload,
            ))
        batch.collided = len(collisions)
        batch.collision_samples = [
            {"seq": r.seq, "key": r.dedup_key} for r in collisions[:5]
        ]
        if collisions and batch.disposition == DedupPolicy.FAIL_BATCH:
            # 整批失败并退回读盘前快照
            first = collisions[0]
            batch.status = ArchiveBatch.STATUS_ROLLED_BACK
            batch.skipped = 0
            batch.landed = 0
            batch.late_summary = (
                f"第{first.seq}条撞键 {first.dedup_key}，"
                f"整批{len(records)}条退回读盘前快照"
            )
            job.records.all().delete()
            job.parsed = 0
            job.landed = 0
            job.status = "待解析"
            job.save(update_fields=["parsed", "landed", "status"])
        else:
            # 跳过并计数
            ArchiveEntry.objects.bulk_create(entries)
            batch.status = ArchiveBatch.STATUS_DONE
            batch.skipped = len(collisions)
            batch.landed = len(entries)
            if collisions:
                seqs = "、".join(str(r.seq) for r in collisions[:10])
                batch.late_summary = f"后到{len(collisions)}条未落档：第{seqs}条"
            else:
                batch.late_summary = "无撞钥，全数落档"
            job.landed = len(entries)
            job.status = "已入库"
            job.save(update_fields=["landed", "status"])
        batch.finished_at = timezone.now()
        batch.save()
    except Exception as exc:  # 后台任何意外都要收尾，不许挂在处理中
        batch.status = ArchiveBatch.STATUS_FAILED
        batch.error = f"{type(exc).__name__}: {exc}"
        batch.finished_at = timezone.now()
        batch.save(update_fields=["status", "error", "finished_at"])
