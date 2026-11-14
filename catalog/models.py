from django.db import models

class ParseJob(models.Model):
    title = models.CharField(max_length=120)
    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default="待解析")
    source_uri = models.CharField(max_length=255)
    parsed = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    # 读盘代次：每收尾一次 +1；同一批（同一代）落档只许成一笔
    parse_epoch = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class DedupPolicy(models.Model):
    """钥匙策略：只许定一种处置写进策略，写完当天不许换。"""

    SKIP = "skip"
    FAIL_BATCH = "fail_batch"
    DISPOSITIONS = [
        (SKIP, "跳过并计数"),
        (FAIL_BATCH, "整批失败并退回读盘前快照"),
    ]

    disposition = models.CharField(max_length=16, choices=DISPOSITIONS)
    decided_on = models.DateField()
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def disposition_name(self):
        return dict(self.DISPOSITIONS).get(self.disposition, self.disposition)


class ParseRecord(models.Model):
    """解析结果：每条带去重钥匙，钥匙空着禁止落档。"""

    job = models.ForeignKey(ParseJob, related_name="records", on_delete=models.CASCADE)
    seq = models.IntegerField()
    dedup_key = models.CharField(max_length=128, blank=True, default="")
    payload = models.CharField(max_length=255)

    class Meta:
        ordering = ["job_id", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["job", "seq"], name="uniq_record_job_seq"),
        ]


class ArchiveBatch(models.Model):
    """落档批次：跳过条数、撞钥条数、处置名、撞钥样例都落在这里。"""

    STATUS_RUNNING = "处理中"
    STATUS_DONE = "已完成"
    STATUS_ROLLED_BACK = "已退回"
    STATUS_FAILED = "处理失败"

    job = models.ForeignKey(ParseJob, related_name="batches", on_delete=models.CASCADE)
    parse_epoch = models.IntegerField()
    batch_seq = models.IntegerField()
    disposition = models.CharField(max_length=16)  # 落档携带的处理法
    status = models.CharField(max_length=16, default=STATUS_RUNNING)
    total = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    skipped = models.IntegerField(default=0)   # 跳过条数
    collided = models.IntegerField(default=0)  # 撞钥条数
    collision_samples = models.JSONField(default=list)  # 撞钥样例
    late_summary = models.CharField(max_length=255, default="")  # 后到失败摘要
    error = models.CharField(max_length=255, default="")
    created_by = models.CharField(max_length=64, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            # 同一批落档两人争抢只成一笔（只记先到）
            models.UniqueConstraint(fields=["job", "parse_epoch"], name="uniq_batch_job_epoch"),
        ]

    @property
    def disposition_name(self):
        return dict(DedupPolicy.DISPOSITIONS).get(self.disposition, self.disposition)


class ArchiveEntry(models.Model):
    """落档库：去重钥匙全局唯一。"""

    batch = models.ForeignKey(ArchiveBatch, related_name="entries", on_delete=models.CASCADE)
    job = models.ForeignKey(ParseJob, related_name="entries", on_delete=models.CASCADE)
    seq = models.IntegerField()
    dedup_key = models.CharField(max_length=128, unique=True)
    payload = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
