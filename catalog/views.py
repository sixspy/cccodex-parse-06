from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from catalog.dedup import finalize_read, get_policy, start_archive, write_policy
from catalog.models import ArchiveBatch, DedupPolicy, ParseJob


def _authed(request) -> bool:
    return request.session.get("user") == settings.ADMIN_USER


def _ctx(request, **extra):
    base = {"lab": settings.LAB_NAME, "user": request.session.get("user")}
    base.update(extra)
    return base


def root(request):
    return HttpResponseRedirect("/jobs/" if _authed(request) else "/login")


@require_http_methods(["GET", "POST"])
def login_page(request):
    err = ""
    if request.method == "POST":
        user = request.POST.get("username", "").strip()
        pwd = request.POST.get("password", "")
        if user == settings.ADMIN_USER and pwd == settings.ADMIN_PASSWORD:
            request.session["user"] = user
            return HttpResponseRedirect("/jobs/")
        err = "账号或密码错误"
    return render(request, "login.html", _ctx(request, error=err))


def logout_view(request):
    request.session.flush()
    return HttpResponseRedirect("/login")


def job_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    items = list(ParseJob.objects.all())
    return render(request, "jobs_list.html", _ctx(request, items=items))


def job_detail(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    return render(request, "job_detail.html", _ctx(
        request,
        job=job,
        records=job.records.all(),
        batches=job.batches.all()[:10],
        policy=get_policy(),
        dispositions=DedupPolicy.DISPOSITIONS,
    ))


@require_POST
def job_parse(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    if job.status in ("待解析", "解析失败"):
        finalize_read(job)  # 读盘收尾：造带去重钥匙的解析结果
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_archive(request, job_id: int):
    """开去重落档：校验通过才开批，去重在后台做。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    disposition = request.POST.get("disposition", "")
    ok, msg, batch = start_archive(job, disposition, request.session.get("user", ""))
    if not ok:
        messages.error(request, msg)
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    messages.success(request, msg)
    return HttpResponseRedirect(f"/dedup/batches/{batch.id}/")


@require_http_methods(["GET", "POST"])
def policy_page(request):
    """钥匙页：只许定一种处置写进策略，写完当天不许换。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    if request.method == "POST":
        ok, msg = write_policy(request.POST.get("disposition", ""))
        messages.success(request, msg) if ok else messages.error(request, msg)
        return HttpResponseRedirect("/dedup/policy/")
    policy = get_policy()
    today = timezone.localdate()
    return render(request, "policy.html", _ctx(
        request,
        policy=policy,
        dispositions=DedupPolicy.DISPOSITIONS,
        today=today,
        locked_today=bool(policy and policy.decided_on >= today),
    ))


def batch_list(request):
    """落档列表：并排写出跳过条数、撞钥条数、处置名。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    batches = ArchiveBatch.objects.select_related("job").all()
    return render(request, "batches.html", _ctx(request, batches=batches))


def batch_detail(request, batch_id: int):
    """撞钥详情：撞钥样例与后到失败摘要。"""
    if not _authed(request):
        return HttpResponseRedirect("/login")
    batch = get_object_or_404(ArchiveBatch.objects.select_related("job"), pk=batch_id)
    return render(request, "batch_detail.html", _ctx(
        request,
        batch=batch,
        entries=batch.entries.all()[:20],
    ))
