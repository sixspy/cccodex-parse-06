"""去重落档验收：跳过或整批退回、处理偏了拦、空钥匙失败、
未收尾不能落档、同批争抢只记先到、落档列表读出去重结果与撞钥样例。"""
import time

import pytest

from catalog.dedup import get_policy
from catalog.models import ArchiveBatch, ArchiveEntry, ParseJob
from catalog.seed import ensure_seed


def login(client):
    r = client.post("/login", {"username": "admin", "password": "123456"})
    assert r.status_code == 302


def job(code):
    return ParseJob.objects.get(code=code)


def wait_done(batch_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        batch = ArchiveBatch.objects.get(id=batch_id)
        if batch.status != "处理中":
            return batch
        time.sleep(0.05)
    raise AssertionError("后台去重未收尾")


def parse_and_policy(client, code, disposition):
    client.post("/dedup/policy/", {"disposition": disposition})
    j = job(code)
    client.post(f"/jobs/{j.id}/parse/")
    return ParseJob.objects.get(code=code)


# ---------- 钥匙策略 ----------

@pytest.mark.django_db
def test_policy_write_once_per_day(client):
    ensure_seed()
    login(client)
    r = client.post("/dedup/policy/", {"disposition": "skip"})
    assert r.status_code == 302
    assert get_policy().disposition == "skip"
    # 写完当天不许换
    r = client.post("/dedup/policy/", {"disposition": "fail_batch"}, follow=True)
    assert "当天不许换" in r.content.decode()
    assert get_policy().disposition == "skip"


@pytest.mark.django_db
def test_policy_page_reads_dispositions(client):
    ensure_seed()
    login(client)
    body = client.get("/dedup/policy/").content.decode()
    assert "跳过并计数" in body and "整批失败并退回读盘前快照" in body


# ---------- 落档前置校验 ----------

@pytest.mark.django_db
def test_archive_forbidden_before_read_finalized(client):
    ensure_seed()
    login(client)
    client.post("/dedup/policy/", {"disposition": "skip"})
    j = job("promo_html")  # 待解析，读盘未收尾
    r = client.post(f"/jobs/{j.id}/archive/", {"disposition": "skip"}, follow=True)
    assert "禁止开去重落档" in r.content.decode()
    assert ArchiveBatch.objects.count() == 0


@pytest.mark.django_db
def test_archive_needs_policy(client):
    ensure_seed()
    login(client)
    j = job("promo_html")
    client.post(f"/jobs/{j.id}/parse/")
    r = client.post(f"/jobs/{j.id}/archive/", {"disposition": "skip"}, follow=True)
    assert "钥匙策略未写" in r.content.decode()
    assert ArchiveBatch.objects.count() == 0


@pytest.mark.django_db
def test_archive_disposition_mismatch_blocked(client):
    ensure_seed()
    login(client)
    j = parse_and_policy(client, "promo_html", "skip")
    # 落档携带的处理法与钥匙策略不一致
    r = client.post(f"/jobs/{j.id}/archive/", {"disposition": "fail_batch"}, follow=True)
    assert "去重处理写歪了" in r.content.decode()
    assert ArchiveBatch.objects.count() == 0


@pytest.mark.django_db
def test_empty_key_forbidden(client):
    ensure_seed()
    login(client)
    j = parse_and_policy(client, "index_json", "skip")
    assert j.records.filter(dedup_key="").exists()  # 空钥匙样例
    r = client.post(f"/jobs/{j.id}/archive/", {"disposition": "skip"}, follow=True)
    assert "钥匙字段空着禁止落档" in r.content.decode()
    assert ArchiveBatch.objects.count() == 0


# ---------- 去重验收：跳过并计数 ----------

@pytest.mark.django_db(transaction=True)
def test_skip_disposition_counts_and_lands(client):
    ensure_seed()
    login(client)
    j = parse_and_policy(client, "promo_html", "skip")
    client.post(f"/jobs/{j.id}/archive/", {"disposition": "skip"})
    batch = wait_done(ArchiveBatch.objects.get().id)
    j.refresh_from_db()
    assert batch.status == "已完成"
    assert batch.collided >= 1
    assert batch.skipped == batch.collided
    assert batch.landed == batch.total - batch.skipped
    assert ArchiveEntry.objects.count() == batch.landed
    assert j.status == "已入库" and j.landed == batch.landed
    assert batch.collision_samples  # 撞钥样例
    assert "后到" in batch.late_summary  # 后到失败摘要


# ---------- 去重验收：整批失败退回读盘前快照 ----------

@pytest.mark.django_db(transaction=True)
def test_fail_batch_rolls_back_to_pre_read_snapshot(client):
    ensure_seed()
    login(client)
    j = parse_and_policy(client, "quote_xlsx", "fail_batch")
    client.post(f"/jobs/{j.id}/archive/", {"disposition": "fail_batch"})
    batch = wait_done(ArchiveBatch.objects.get().id)
    j.refresh_from_db()
    assert batch.status == "已退回"
    assert batch.collided >= 1
    assert ArchiveEntry.objects.count() == 0
    assert j.status == "待解析" and j.parsed == 0 and j.landed == 0
    assert j.records.count() == 0
    assert "退回读盘前快照" in batch.late_summary


# ---------- 同批争抢只记先到 ----------

@pytest.mark.django_db
def test_same_batch_contention_first_wins(client):
    ensure_seed()
    login(client)
    j = parse_and_policy(client, "promo_html", "skip")
    # 先到者已占下这一批（同一读盘代次）
    ArchiveBatch.objects.create(
        job=j, parse_epoch=j.parse_epoch, batch_seq=1,
        disposition="skip", created_by="rival",
    )
    r = client.post(f"/jobs/{j.id}/archive/", {"disposition": "skip"}, follow=True)
    body = r.content.decode()
    assert "只记先到" in body or "只成一笔" in body
    assert ArchiveBatch.objects.count() == 1
    assert ArchiveBatch.objects.get().created_by == "rival"


# ---------- 落档列表与撞钥详情 ----------

@pytest.mark.django_db(transaction=True)
def test_batch_list_and_detail_show_dedup_results(client):
    ensure_seed()
    login(client)
    j = parse_and_policy(client, "quote_xlsx", "skip")
    client.post(f"/jobs/{j.id}/archive/", {"disposition": "skip"})
    batch = wait_done(ArchiveBatch.objects.get().id)

    body = client.get("/dedup/batches/").content.decode()
    assert "跳过条数" in body and "撞钥条数" in body and "处置名" in body
    assert "跳过并计数" in body

    body = client.get(f"/dedup/batches/{batch.id}/").content.decode()
    assert "撞钥样例" in body and "后到失败摘要" in body
    assert "quote_xlsx-001" in body  # 撞钥样例钥匙
    assert "后到" in body
