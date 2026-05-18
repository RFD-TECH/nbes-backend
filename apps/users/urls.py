"""apps/users/urls.py — Phase 1 / Sprint 1.1 endpoints.

Mounted in config/urls.py:
    /api/v1/auth/...   (login, mfa, refresh, logout, accept-invite, mfa/totp/...)
    /api/v1/me
    /api/v1/admin/users/...
"""
from django.urls import path

from apps.users.views import (
    AcceptInviteView,
    AdminUserDetailView,
    AdminUserListCreateView,
    AdminUserMFAResetView,
    LoginView,
    LogoutView,
    MFAVerifyView,
    MeView,
    RefreshView,
    TOTPEnrolConfirmView,
    TOTPEnrolStartView,
    WebAuthnRegisterBeginView,
    WebAuthnRegisterFinishView,
)


auth_patterns = [
    path("login",          LoginView.as_view(),    name="auth-login"),
    path("mfa",            MFAVerifyView.as_view(), name="auth-mfa"),
    path("refresh",        RefreshView.as_view(),  name="auth-refresh"),
    path("logout",         LogoutView.as_view(),   name="auth-logout"),
    path("accept-invite",  AcceptInviteView.as_view(), name="auth-accept-invite"),

    path("mfa/totp/enroll",  TOTPEnrolStartView.as_view(),   name="mfa-totp-enroll"),
    path("mfa/totp/confirm", TOTPEnrolConfirmView.as_view(), name="mfa-totp-confirm"),
    path("mfa/webauthn/register/begin",
         WebAuthnRegisterBeginView.as_view(),  name="mfa-webauthn-begin"),
    path("mfa/webauthn/register/finish",
         WebAuthnRegisterFinishView.as_view(), name="mfa-webauthn-finish"),
]


me_patterns = [
    path("",  MeView.as_view(), name="me"),
]


admin_user_patterns = [
    path("",                              AdminUserListCreateView.as_view(), name="admin-user-list-create"),
    path("<uuid:user_id>",                AdminUserDetailView.as_view(),     name="admin-user-detail"),
    path("<uuid:user_id>/mfa/reset",      AdminUserMFAResetView.as_view(),   name="admin-user-mfa-reset"),
]
