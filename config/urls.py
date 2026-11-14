from django.urls import path
from catalog import views

urlpatterns = [
    path("", views.root),
    path("login", views.login_page, name="login"),
    path("logout", views.logout_view),
    path("jobs/", views.job_list),
    path("jobs/<int:job_id>/", views.job_detail),
    path("jobs/<int:job_id>/parse/", views.job_parse),
    path("jobs/<int:job_id>/archive/", views.job_archive),
    path("dedup/policy/", views.policy_page),
    path("dedup/batches/", views.batch_list),
    path("dedup/batches/<int:batch_id>/", views.batch_detail),
]
