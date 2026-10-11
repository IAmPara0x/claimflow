from django.urls import path

from . import views

app_name = "claims"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("claims/", views.claim_list, name="list"),
    path("claims/new/", views.claim_create, name="create"),
    path("claims/<str:claim_number>/", views.claim_detail, name="detail"),
    path("claims/<str:claim_number>/edit/", views.claim_edit, name="edit"),
    path("claims/<str:claim_number>/submit/", views.claim_submit, name="submit"),
    path("claims/<str:claim_number>/decide/", views.claim_decide, name="decide"),
]
