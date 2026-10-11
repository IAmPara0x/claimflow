from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from claims import api

router = DefaultRouter()
router.register("claims", api.ClaimViewSet, basename="claim")
router.register("procedures", api.ProcedureCodeViewSet, basename="procedure")
router.register("diagnoses", api.DiagnosisCodeViewSet, basename="diagnosis")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/", include(router.urls)),
    path("api/token/", obtain_auth_token, name="api-token"),
    path("", include("claims.urls")),
]
