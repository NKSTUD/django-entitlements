from django.contrib import admin
from django.http import HttpResponse
from django.urls import path

import entitlements


@entitlements.requires("exports")
def export(request):
    return HttpResponse("ok", status=int(request.GET.get("status", 200)))


@entitlements.requires("api")
def api(request):
    return HttpResponse("ok")


urlpatterns = [path("admin/", admin.site.urls), path("export/", export), path("api/", api)]
