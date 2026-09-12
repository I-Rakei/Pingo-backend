from django.urls import path

from . import views

urlpatterns = [
    path("bootstrap/", views.bootstrap_view),
    path("dashboard/summary/", views.dashboard_summary_view),
    path("dashboard/debts/", views.dashboard_debts_view),
    path("auth/csrf/", views.csrf),
    path("auth/login/", views.login_view),
    path("auth/register/", views.register_view),
    path("auth/logout/", views.logout_view),
    path("mobile/register/", views.mobile_register_view),
    path("mobile/login/", views.mobile_login_view),
    path("mobile/sync/", views.mobile_sync_view),
    path("push/config/", views.push_config_view),
    path("push/subscription/", views.push_subscription_view),
    path("push/test/", views.push_test_view),
    path("auth/me/", views.me_view),
    path("preferences/", views.preferences_view),
    path("clients/", views.clients_view),
    path("clients/<int:client_id>/", views.client_detail_view),
    path("debts/", views.debts_view),
    path("debts/<str:reference>/", views.debt_detail_view),
    path("debts/<str:reference>/payments/", views.debt_payment_view),
    path("debts/<str:reference>/installments/<int:number>/revert/", views.installment_revert_view),
    path("payments/", views.payments_view),
]
